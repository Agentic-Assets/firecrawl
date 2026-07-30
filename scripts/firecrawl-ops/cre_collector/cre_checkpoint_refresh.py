#!/usr/bin/env python3
"""Resumable, source-checkpointed full CRE listing refresh.

Each source is collected into its own artifact, validated fail-closed, checked
against the live read-only coverage gate, dry-run through the ingestor, and then
ingested additively.  A manifest is atomically updated after every durable
transition so an interrupted run can resume without recollecting or reingesting
already completed sources.

This runner never passes --monitor, --mark-missing, --activate-status, or
--update-baseline.  It shares the normal CRE tier lock and does not manage
launchd.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from cre_ingest import (
    AUTHORITATIVE_INVENTORY_FEED_SOURCE_KEYS,
    CHILD_PRESERVING_AUTHORITATIVE_FEED_SOURCE_KEYS,
    INVENTORY_ONLY_SOURCE_DEFINITIONS,
    SOURCE_TO_BROKERAGE,
    STRICT_FRESHNESS_SOURCE_KEYS,
    database_target_fingerprint_from_url,
    load_db_url,
    merge_rows,
    to_inventory_only_row,
    to_row,
)


COLLECTOR_DIR = Path(__file__).resolve().parent
REPO_ROOT = COLLECTOR_DIR.parents[2]
DEFAULT_OUT_ROOT = COLLECTOR_DIR / "out" / "checkpoint-refresh"
SCHEMA_VERSION = 2
DEFAULT_MAX_RESUME_AGE_HOURS = 24.0
MAX_FUTURE_CLOCK_SKEW = timedelta(minutes=5)
SOURCE_KEYS = tuple(SOURCE_TO_BROKERAGE)
TRANSACTIONS = ("sale", "lease")
PROPERTY_DETAIL_FRESHNESS_SOURCE_KEYS = {"avison-young"}
FORBIDDEN_INGEST_FLAGS = {
    "--mark-missing",
    "--activate-status",
    "--no-mark-missing",
    "--update-baseline",
}


class RefreshError(RuntimeError):
    """Base runner error."""


class ArtifactValidationError(RefreshError):
    """Collector artifact violated the full-source contract."""


class LockHeldError(RefreshError):
    """Another CRE tier owns the shared lock."""


class GlobalStageError(RefreshError):
    """A shared infrastructure or live-write stage failed."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso8601(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactValidationError(f"{field} must be a nonempty ISO-8601 timestamp")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ArtifactValidationError(f"{field} is not valid ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ArtifactValidationError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_owner(lock_dir: Path) -> int | None:
    try:
        first = (lock_dir / "pid").read_text(encoding="utf-8").split()[0]
        return int(first)
    except (FileNotFoundError, IndexError, ValueError, OSError):
        return None


def canonical_shared_lock_dir(repo_root: Path = REPO_ROOT) -> Path:
    """Resolve the primary checkout's CRE lock, including from a worktree."""
    common_git_dir = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    primary_checkout = Path(common_git_dir).resolve().parent
    return (
        primary_checkout
        / "scripts"
        / "firecrawl-ops"
        / "cre_collector"
        / "out"
        / "daily"
        / ".cre.lock"
    )


@dataclass
class SharedLock:
    path: Path
    held: bool = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.mkdir()
        except FileExistsError:
            owner = _lock_owner(self.path)
            if owner is None or _pid_alive(owner):
                detail = "owner is starting" if owner is None else f"live owner pid {owner}"
                raise LockHeldError(f"CRE lock is held ({detail}): {self.path}")
            reclaim = Path(f"{self.path}.reclaim")
            try:
                reclaim.mkdir()
            except FileExistsError as exc:
                raise LockHeldError(f"CRE lock reclamation is already in progress: {self.path}") from exc
            try:
                current = _lock_owner(self.path)
                if current is not None and _pid_alive(current):
                    raise LockHeldError(f"CRE lock became live during reclaim (pid {current})")
                shutil.rmtree(self.path, ignore_errors=True)
                self.path.mkdir()
            finally:
                shutil.rmtree(reclaim, ignore_errors=True)
        (self.path / "pid").write_text(
            f"{os.getpid()} {int(datetime.now(timezone.utc).timestamp())}\n",
            encoding="utf-8",
        )
        self.held = True

    def release(self) -> None:
        if self.held and _lock_owner(self.path) == os.getpid():
            shutil.rmtree(self.path, ignore_errors=True)
        self.held = False

    def __enter__(self) -> "SharedLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


def build_collect_argv(
    source: str,
    output: Path,
    *,
    page_cap: int = 400,
    concurrency: int = 3,
    transactions: Sequence[str] = TRANSACTIONS,
) -> list[str]:
    if source not in SOURCE_KEYS:
        raise ValueError(f"unknown source: {source}")
    selected_transactions = tuple(transactions)
    if not selected_transactions or any(
        transaction not in TRANSACTIONS for transaction in selected_transactions
    ):
        raise ValueError(f"invalid transaction selection: {selected_transactions}")
    transaction_arg = (
        "both"
        if selected_transactions == TRANSACTIONS
        else selected_transactions[0]
        if len(selected_transactions) == 1
        else None
    )
    if transaction_arg is None:
        raise ValueError(
            "transaction selection must be sale, lease, or canonical sale+lease"
        )
    return [
        "npx",
        "tsx",
        "collect.ts",
        f"--source={source}",
        f"--transaction={transaction_arg}",
        "--max-items=0",
        f"--page-cap={page_cap}",
        f"--concurrency={concurrency}",
        f"--out={output}",
    ]


def build_gate_argv(
    artifact: Path,
    output: Path,
    env_file: str | None,
    *,
    expected_db_target_sha256: str | None = None,
) -> list[str]:
    argv = [
        sys.executable,
        "cre_gate.py",
        "--in",
        str(artifact),
        "--apply",
        "--strict",
        "--out",
        str(output),
    ]
    if env_file:
        argv.extend(["--env-file", env_file])
    if expected_db_target_sha256:
        argv.extend(
            ["--expected-db-target-sha256", expected_db_target_sha256]
        )
    return argv


def build_ingest_dry_run_argv(
    artifact: Path,
    sql_dir: Path,
    *,
    require_strict_freshness: bool = False,
) -> list[str]:
    argv = [
        sys.executable,
        "cre_ingest.py",
        "--in",
        str(artifact),
        "--dry-run",
        "--keep-artifacts",
        str(sql_dir),
    ]
    if require_strict_freshness:
        argv.append("--require-strict-freshness")
    return argv


def build_ingest_argv(
    artifact: Path,
    env_file: str | None,
    *,
    require_strict_freshness: bool = False,
    expected_db_target_sha256: str | None = None,
) -> list[str]:
    argv = [sys.executable, "cre_ingest.py", "--in", str(artifact)]
    if require_strict_freshness:
        argv.append("--require-strict-freshness")
    if env_file:
        argv.extend(["--env-file", env_file])
    if expected_db_target_sha256:
        argv.extend(
            ["--expected-db-target-sha256", expected_db_target_sha256]
        )
    if FORBIDDEN_INGEST_FLAGS.intersection(argv):
        raise AssertionError("additive ingest argv contains a forbidden flag")
    return argv


def build_validate_argv(
    output: Path,
    env_file: str | None,
    *,
    expected_db_target_sha256: str | None = None,
) -> list[str]:
    argv = [
        sys.executable,
        "cre_validate.py",
        "--format",
        "json",
        "--out",
        str(output),
    ]
    if env_file:
        argv.extend(["--env-file", env_file])
    if expected_db_target_sha256:
        argv.extend(
            ["--expected-db-target-sha256", expected_db_target_sha256]
        )
    return argv


def safe_process_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.pop("CRE_ACTIVATE_STATUS", None)
    return env


def fresh_source_env(
    source: str,
    run_dir: Path,
    base: Mapping[str, str] | None = None,
    generation_started_at: str | None = None,
    attempt_number: int = 1,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return subprocess env plus a nonsecret manifest summary of overrides."""
    env = safe_process_env(base)
    overrides: dict[str, str] = {}

    def set_value(name: str, value: str) -> None:
        env[name] = value
        overrides[name] = value

    def clear(name: str) -> None:
        env.pop(name, None)
        overrides[name] = "<unset>"

    set_value("CRE_REFRESH_GENERATION", run_dir.name)
    if generation_started_at:
        set_value("CRE_REFRESH_STARTED_AT", generation_started_at)
    if source in STRICT_FRESHNESS_SOURCE_KEYS:
        set_value("CRE_REQUIRE_FRESH_DETAILS", "1")
    else:
        clear("CRE_REQUIRE_FRESH_DETAILS")
    if source == "avison-young":
        set_value("CRE_REQUIRE_FRESH_PROPERTY_DETAILS", "1")
    else:
        clear("CRE_REQUIRE_FRESH_PROPERTY_DETAILS")

    if source in {"svn", "lee-associates", "franklin-street"}:
        clear("BUILDOUT_CACHE_ONLY")
        clear("BUILDOUT_ASSEMBLE_FROM_CACHE")
        clear("BUILDOUT_USE_PAGE_CACHE")
        if attempt_number > 1:
            # A strict identity/count failure can reflect a provider feed that
            # moved across page boundaries during collection. Re-reading the
            # same generation cache would deterministically repeat the failed
            # snapshot, so bounded retries must obtain a new live snapshot.
            set_value("BUILDOUT_REFRESH_PAGE_CACHE", "1")
        else:
            clear("BUILDOUT_REFRESH_PAGE_CACHE")
        set_value("BUILDOUT_CACHE_DIR", str(run_dir / "cache" / "buildout"))
    if source == "jll":
        set_value("JLL_DETAIL_CACHE_DIR", str(run_dir / "cache" / "jll-detail"))
        if generation_started_at:
            set_value("JLL_DETAIL_CACHE_MIN_CACHED_AT", generation_started_at)
    if source == "jll-investor":
        set_value("JLL_INVESTOR_SITEMAP_SCAN_LIMIT", "0")
    if source == "avison-young":
        set_value("AVISON_YOUNG_DETAIL_LIMIT", "1000000")
        set_value("AVISON_YOUNG_DETAIL_TRANSPORT", "direct")
    if source == "cushman-wakefield":
        clear("CUSHMAN_QUERY")
        set_value("CUSHMAN_DETAIL_MODE", "base")
    if source == "colliers-main":
        set_value(
            "COLLIERS_MAIN_DETAIL_CACHE_PATH",
            str(run_dir / "cache" / "colliers-main" / "detail-cache.jsonl"),
        )
        set_value("COLLIERS_MAIN_MAX_FETCHES_PER_RUN", "2500")
        set_value("NODE_OPTIONS", "--max-old-space-size=6144")
    return env, overrides


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError(f"invalid JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise ArtifactValidationError("artifact must be a JSON object")
    return value


def compute_staged_stats(data: Mapping[str, Any]) -> dict[str, int]:
    run_meta = data.get("runMeta")
    if not isinstance(run_meta, dict):
        raise ArtifactValidationError("runMeta must be an object")
    scraped_at = run_meta.get("finishedAt")
    brokers = data.get("brokers")
    if not isinstance(brokers, list):
        raise ArtifactValidationError("brokers must be an array")
    brokers_by_idx = {index: broker for index, broker in enumerate(brokers)}
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    rejected = 0
    detail_errors = 0
    detail_unavailable = 0
    provisional_identities = 0
    inventory_only: set[tuple[str, str]] = set()
    listings = data.get("listings")
    if not isinstance(listings, list):
        raise ArtifactValidationError("listings must be an array")
    for index, listing in enumerate(listings):
        if not isinstance(listing, dict):
            raise ArtifactValidationError(f"listings[{index}] must be an object")
        if listing.get("detailError"):
            detail_errors += 1
        if listing.get("detailUnavailable"):
            detail_unavailable += 1
            if listing.get("preserveChildCollections") is not True:
                raise ArtifactValidationError(
                    f"listings[{index}] has detailUnavailable without child preservation"
                )
        if listing.get("provisionalIdentity"):
            provisional_identities += 1
        inventory_row = to_inventory_only_row(listing, scraped_at)
        if (
            listing.get("inventoryOnly") is not None
            and inventory_row is None
        ):
            raise ArtifactValidationError(
                f"listings[{index}] has an invalid inventoryOnly identity"
            )
        if inventory_row is not None:
            inventory_key = (
                inventory_row["slug"],
                inventory_row["external_id"],
            )
            if (
                listing.get("sourceKey") == "colliers"
                and inventory_key in inventory_only
            ):
                raise ArtifactValidationError(
                    "Colliers artifact contains duplicate provisional "
                    f"inventory identity {inventory_key[1]!r}"
                )
            inventory_only.add(inventory_key)
            continue
        try:
            row = to_row(listing, brokers_by_idx, scraped_at)
        except Exception as exc:
            raise ArtifactValidationError(f"listings[{index}] failed to_row: {exc}") from exc
        if row is None:
            rejected += 1
            continue
        key = (row["slug"], row["external_id"])
        if (
            listing.get("sourceKey") in {"colliers", "newmark"}
            and key in merged
        ):
            identity_label = (
                "canonical ProjectId"
                if listing.get("sourceKey") == "colliers"
                else "canonical identity"
            )
            raise ArtifactValidationError(
                f"{listing.get('sourceKey')} artifact contains duplicate "
                f"{identity_label} "
                f"{key[1]!r}"
            )
        merged[key] = merge_rows(merged[key], row) if key in merged else row
    return {
        "flat_listings": len(listings),
        "staged_unique": len(merged),
        "rejected_by_ingest": rejected,
        "detail_errors": detail_errors,
        "detail_unavailable": detail_unavailable,
        "provisional_identities": provisional_identities,
        "inventory_only": len(inventory_only),
    }


def validate_source_artifact(
    path: Path,
    expected_source: str,
    attempt_started_at: str | datetime,
    *,
    require_strict_freshness: bool = False,
    expected_generation_id: str | None = None,
    expected_generation_started_at: str | datetime | None = None,
    expected_transactions: Sequence[str] = TRANSACTIONS,
    now: datetime | None = None,
) -> dict[str, Any]:
    if expected_source not in SOURCE_KEYS:
        raise ArtifactValidationError(f"unknown expected source: {expected_source}")
    data = _load_json(path)
    run_meta = data.get("runMeta")
    if not isinstance(run_meta, dict):
        raise ArtifactValidationError("runMeta must be an object")
    if run_meta.get("mode") != "full":
        raise ArtifactValidationError("runMeta.mode must be 'full'")
    selected_transactions = tuple(expected_transactions)
    if (
        not selected_transactions
        or any(transaction not in TRANSACTIONS for transaction in selected_transactions)
        or len(selected_transactions) != len(set(selected_transactions))
    ):
        raise ArtifactValidationError("expected transaction scope is invalid")
    if run_meta.get("transactions") != list(selected_transactions):
        raise ArtifactValidationError(
            f"runMeta.transactions must be {list(selected_transactions)!r}"
        )
    if run_meta.get("maxItemsPerSource") is not None:
        raise ArtifactValidationError("full refresh requires unlimited maxItemsPerSource")

    started = parse_iso8601(run_meta.get("startedAt"), field="runMeta.startedAt")
    finished = parse_iso8601(run_meta.get("finishedAt"), field="runMeta.finishedAt")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    latest_allowed = current + MAX_FUTURE_CLOCK_SKEW
    if started > latest_allowed or finished > latest_allowed:
        raise ArtifactValidationError(
            "artifact timestamps exceed the 5-minute clock-skew allowance"
        )
    attempt = (
        attempt_started_at.astimezone(timezone.utc)
        if isinstance(attempt_started_at, datetime)
        else parse_iso8601(attempt_started_at, field="attempt_started_at")
    )
    if finished < started:
        raise ArtifactValidationError("runMeta.finishedAt precedes startedAt")
    if started.timestamp() + 5 < attempt.timestamp():
        raise ArtifactValidationError("artifact predates the current collection attempt")

    freshness = run_meta.get("freshness")
    strict_freshness = (
        isinstance(freshness, dict)
        and freshness.get("requireFreshDetails") is True
    )
    property_detail_freshness = (
        expected_source in PROPERTY_DETAIL_FRESHNESS_SOURCE_KEYS
        and isinstance(freshness, dict)
        and freshness.get("requireFreshPropertyDetails") is True
        and not strict_freshness
    )
    if (
        expected_source in PROPERTY_DETAIL_FRESHNESS_SOURCE_KEYS
        and not strict_freshness
        and not property_detail_freshness
    ):
        raise ArtifactValidationError(
            f"{expected_source} requires "
            "runMeta.freshness.requireFreshPropertyDetails=true"
        )
    if require_strict_freshness and not strict_freshness:
        raise ArtifactValidationError(
            f"{expected_source} requires runMeta.freshness.requireFreshDetails=true"
        )
    generation_started = None
    generation_id = None
    if strict_freshness or property_detail_freshness:
        if not isinstance(freshness, dict):
            raise ArtifactValidationError(
                f"{expected_source} requires runMeta.freshness generation metadata"
            )
        generation_id = freshness.get("generationId")
        if not isinstance(generation_id, str) or not generation_id.strip():
            raise ArtifactValidationError(
                "generation-backed freshness requires runMeta.freshness.generationId"
            )
        generation_started = parse_iso8601(
            freshness.get("generationStartedAt"),
            field="runMeta.freshness.generationStartedAt",
        )
        if generation_started > latest_allowed:
            raise ArtifactValidationError(
                "refresh generation start exceeds the 5-minute clock-skew allowance"
            )
        if (
            expected_generation_id is not None
            and generation_id != expected_generation_id
        ):
            raise ArtifactValidationError(
                "runMeta.freshness.generationId does not match the checkpoint generation"
            )
        if expected_generation_started_at is not None:
            if isinstance(expected_generation_started_at, datetime):
                if expected_generation_started_at.tzinfo is None:
                    raise ArtifactValidationError(
                        "expected_generation_started_at must include a timezone"
                    )
                expected_started = expected_generation_started_at.astimezone(
                    timezone.utc
                )
            else:
                expected_started = parse_iso8601(
                    expected_generation_started_at,
                    field="expected_generation_started_at",
                )
            if generation_started != expected_started:
                raise ArtifactValidationError(
                    "runMeta.freshness.generationStartedAt does not match "
                    "the checkpoint generation"
                )

    entries = data.get("sources")
    if not isinstance(entries, list) or len(entries) != len(selected_transactions):
        if selected_transactions == TRANSACTIONS:
            raise ArtifactValidationError(
                "single-source full artifact must contain two source entries"
            )
        raise ArtifactValidationError(
            "single-source artifact must contain exactly one entry per selected transaction"
        )
    seen_transactions: set[str] = set()
    entry_total = 0
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ArtifactValidationError(f"sources[{index}] must be an object")
        if entry.get("sourceKey") != expected_source:
            raise ArtifactValidationError(f"sources[{index}] has the wrong sourceKey")
        tx = entry.get("transaction")
        if tx not in selected_transactions or tx in seen_transactions:
            raise ArtifactValidationError(
                "source entries must match the selected transactions exactly once"
            )
        seen_transactions.add(tx)
        if entry.get("supported") is not True:
            raise ArtifactValidationError(f"{expected_source}/{tx} is not supported")
        if entry.get("error"):
            raise ArtifactValidationError(f"{expected_source}/{tx} reported an error")
        if entry.get("truncated") is not False:
            raise ArtifactValidationError(
                f"{expected_source}/{tx} must explicitly report truncated=false"
            )
        count = entry.get("listingsCollected")
        if not isinstance(count, int) or count < 0:
            raise ArtifactValidationError(f"{expected_source}/{tx} has an invalid listing count")
        entry_total += count
        if strict_freshness or property_detail_freshness:
            metrics = entry.get("freshness")
            if not isinstance(metrics, dict):
                raise ArtifactValidationError(
                    f"{expected_source}/{tx} is missing freshness admission metrics"
                )
            if metrics.get("listings") != count:
                raise ArtifactValidationError(
                    f"{expected_source}/{tx} freshness listing count does not match"
                )
            for metric in (
                "detailErrors",
                "staleInventoryObservations",
                "staleDetailObservations",
            ):
                if metrics.get(metric) != 0:
                    raise ArtifactValidationError(
                        f"{expected_source}/{tx} freshness metric {metric} must be zero"
                    )
            if metrics.get("inventoryObserved") != count:
                raise ArtifactValidationError(
                    f"{expected_source}/{tx} lacks current inventory observations"
                )
            child_preservation_rows = metrics.get("childPreservationRows")
            if property_detail_freshness:
                if child_preservation_rows not in {0, count}:
                    raise ArtifactValidationError(
                        f"{expected_source}/{tx} has partial child-preservation coverage"
                    )
                if metrics.get("detailObserved") != count:
                    raise ArtifactValidationError(
                        f"{expected_source}/{tx} lacks current property-detail observations"
                    )
                if metrics.get("authoritativeInventoryFeed") != 0:
                    raise ArtifactValidationError(
                        f"{expected_source}/{tx} incorrectly claims authoritative inventory detail"
                    )
            elif (
                expected_source
                in CHILD_PRESERVING_AUTHORITATIVE_FEED_SOURCE_KEYS
            ):
                if child_preservation_rows != count:
                    raise ArtifactValidationError(
                        f"{expected_source}/{tx} must preserve every child collection"
                    )
            elif child_preservation_rows != 0:
                raise ArtifactValidationError(
                    f"{expected_source}/{tx} must not preserve child collections"
                )
            if property_detail_freshness:
                pass
            elif expected_source in AUTHORITATIVE_INVENTORY_FEED_SOURCE_KEYS:
                if metrics.get("authoritativeInventoryFeed") != count:
                    raise ArtifactValidationError(
                        f"{expected_source}/{tx} is not proven by its authoritative inventory feed"
                    )
            elif metrics.get("detailObserved") != count:
                raise ArtifactValidationError(
                    f"{expected_source}/{tx} lacks detail observations"
                )
    if seen_transactions != set(selected_transactions):
        raise ArtifactValidationError(
            "source entries do not cover the selected transaction scope"
        )

    listings = data.get("listings")
    if not isinstance(listings, list):
        raise ArtifactValidationError("full source artifact listings must be an array")
    if (
        not listings
        and expected_source not in INVENTORY_ONLY_SOURCE_DEFINITIONS
    ):
        raise ArtifactValidationError("full source artifact must contain listings")
    if data.get("totalListings") != len(listings):
        raise ArtifactValidationError("totalListings does not match listings length")
    if entry_total != len(listings):
        raise ArtifactValidationError("source entry counts do not match listings length")
    for index, listing in enumerate(listings):
        if not isinstance(listing, dict):
            raise ArtifactValidationError(f"listings[{index}] must be an object")
        if listing.get("sourceKey") != expected_source:
            raise ArtifactValidationError(f"listings[{index}] has the wrong sourceKey")
        if listing.get("transactionMode") not in selected_transactions:
            raise ArtifactValidationError(f"listings[{index}] has an invalid transactionMode")
        observation_fields = [
            ("inventoryObservedAt", listing.get("inventoryObservedAt")),
            ("detailObservedAt", listing.get("detailObservedAt")),
        ]
        listing_provenance = listing.get("freshnessProvenance")
        if isinstance(listing_provenance, dict):
            observation_fields.append(
                (
                    "freshnessProvenance.validatedAt",
                    listing_provenance.get("validatedAt"),
                )
            )
        for field, value in observation_fields:
            if value is None:
                continue
            observed = parse_iso8601(
                value,
                field=f"listings[{index}].{field}",
            )
            if observed > latest_allowed:
                raise ArtifactValidationError(
                    f"listings[{index}].{field} exceeds "
                    "the 5-minute clock-skew allowance"
                )
        if strict_freshness or property_detail_freshness:
            assert generation_started is not None
            inventory_observed = parse_iso8601(
                listing.get("inventoryObservedAt"),
                field=f"listings[{index}].inventoryObservedAt",
            )
            if inventory_observed < generation_started:
                raise ArtifactValidationError(
                    f"listings[{index}] inventory observation predates the refresh generation"
                )
            if inventory_observed > latest_allowed:
                raise ArtifactValidationError(
                    f"listings[{index}] inventory observation exceeds "
                    "the 5-minute clock-skew allowance"
                )
            provenance = listing.get("freshnessProvenance")
            if not isinstance(provenance, dict):
                raise ArtifactValidationError(
                    f"listings[{index}] is missing freshnessProvenance"
                )
            if provenance.get("generationId") != generation_id:
                raise ArtifactValidationError(
                    f"listings[{index}] belongs to a different refresh generation"
                )
            if listing.get("detailError"):
                raise ArtifactValidationError(
                    f"listings[{index}] cannot satisfy property-detail freshness"
                )
            preserves_children = listing.get("preserveChildCollections") is True
            if property_detail_freshness:
                preserves_with_detail = (
                    listing.get("detailObservedWithChildPreservation") is True
                )
                if preserves_children != preserves_with_detail:
                    raise ArtifactValidationError(
                        f"listings[{index}] has inconsistent child-preservation "
                        "detail proof"
                    )
                if listing.get("detailUnavailable"):
                    raise ArtifactValidationError(
                        f"listings[{index}] has unavailable property detail"
                    )
                if provenance.get("detailScope") != "detail_page":
                    raise ArtifactValidationError(
                        f"listings[{index}] lacks property-detail provenance"
                    )
                if provenance.get("cacheDisposition") != "live":
                    raise ArtifactValidationError(
                        f"listings[{index}] property detail was not observed live"
                    )
                detail_observed = parse_iso8601(
                    listing.get("detailObservedAt"),
                    field=f"listings[{index}].detailObservedAt",
                )
                if detail_observed < generation_started:
                    raise ArtifactValidationError(
                        f"listings[{index}] detail observation predates "
                        "the refresh generation"
                    )
                if detail_observed > latest_allowed:
                    raise ArtifactValidationError(
                        f"listings[{index}] detail observation exceeds "
                        "the 5-minute clock-skew allowance"
                    )
            elif expected_source in AUTHORITATIVE_INVENTORY_FEED_SOURCE_KEYS:
                if provenance.get("detailScope") != "authoritative_inventory_feed":
                    raise ArtifactValidationError(
                        f"listings[{index}] lacks authoritative inventory-feed provenance"
                    )
                if (
                    expected_source
                    in CHILD_PRESERVING_AUTHORITATIVE_FEED_SOURCE_KEYS
                ):
                    if not preserves_children:
                        raise ArtifactValidationError(
                            f"listings[{index}] must preserve child collections"
                        )
                elif preserves_children:
                    raise ArtifactValidationError(
                        f"listings[{index}] must not preserve child collections"
                    )
            else:
                if preserves_children:
                    raise ArtifactValidationError(
                        f"listings[{index}] must not preserve child collections"
                    )
                detail_value = listing.get("detailObservedAt")
                if (
                    provenance.get("cacheDisposition") == "source_revision_cache"
                    and provenance.get("validatedAt")
                ):
                    detail_value = provenance.get("validatedAt")
                detail_observed = parse_iso8601(
                    detail_value,
                    field=f"listings[{index}].detailObservedAt",
                )
                if detail_observed < generation_started:
                    raise ArtifactValidationError(
                        f"listings[{index}] detail observation predates the refresh generation"
                    )
                if detail_observed > latest_allowed:
                    raise ArtifactValidationError(
                        f"listings[{index}] detail observation exceeds "
                        "the 5-minute clock-skew allowance"
                    )

    stats = compute_staged_stats(data)
    if stats["rejected_by_ingest"]:
        raise ArtifactValidationError(
            f"{stats['rejected_by_ingest']} listing(s) would be rejected by ingest"
        )
    if stats["detail_errors"]:
        raise ArtifactValidationError(
            f"{stats['detail_errors']} listing(s) contain detailError"
        )
    if (
        expected_source in {"colliers", "newmark"}
        and stats["flat_listings"]
        != stats["staged_unique"] + stats["inventory_only"]
    ):
        raise ArtifactValidationError(
            f"{expected_source} artifact does not preserve a one-to-one provider-card "
            "identity across canonical and inventory-only rows"
        )
    if (
        stats["staged_unique"] <= 0
        and stats["inventory_only"] <= 0
        and not (
            expected_source in INVENTORY_ONLY_SOURCE_DEFINITIONS
            and not listings
        )
    ):
        raise ArtifactValidationError("artifact has no usable unique rows")
    return {
        **stats,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "strict_freshness": strict_freshness,
        "property_detail_freshness": property_detail_freshness,
        "freshness_generation_id": generation_id,
        "freshness_generation_started_at": (
            generation_started.isoformat()
            if generation_started is not None
            else None
        ),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def run_command(
    argv: Sequence[str],
    log_path: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"[{utc_now()}] command: {' '.join(argv)}\n")
        log.flush()
        proc = subprocess.run(
            list(argv),
            cwd=COLLECTOR_DIR,
            env=dict(env) if env is not None else None,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        log.write(f"[{utc_now()}] rc={proc.returncode}\n")
    return proc.returncode


def git_identity() -> tuple[str, bool]:
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    return sha, dirty


def database_target_fingerprint(env_file: str | None) -> dict[str, str]:
    """Return a credential-free identity for the selected PostgreSQL target."""
    db_url, _env_path = load_db_url(env_file)
    try:
        return database_target_fingerprint_from_url(db_url)
    except ValueError as exc:
        raise RefreshError(f"cannot bind database target: {exc}") from exc


def new_manifest(
    run_dir: Path,
    *,
    git_sha: str,
    git_dirty: bool,
    sources: Sequence[str],
    page_cap: int,
    concurrency: int,
    transactions: Sequence[str] = TRANSACTIONS,
    database_target: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_dir.name,
        "status": "running",
        "started_at": now,
        "updated_at": now,
        "finished_at": None,
        "collector_git_sha": git_sha,
        "collector_git_dirty": git_dirty,
        "scope": {
            "kind": (
                "collector_registry"
                if tuple(transactions) == TRANSACTIONS
                else "collector_registry_transaction_subset"
            ),
            "source_keys": list(sources),
            "unsupported_active_rows_before": None,
        },
        "config": {
            "sources": list(sources),
            "transactions": list(transactions),
            "max_items": 0,
            "page_cap": page_cap,
            "concurrency": concurrency,
            "additive": True,
            "status_activation": False,
            "mark_missing": False,
        },
        "preflight": {
            "database_target": dict(database_target) if database_target else None,
        },
        "sources": {
            source: {
                "state": "pending",
                "attempts": [],
                "artifact": None,
                "gate": None,
                "dry_run": None,
                "ingest": None,
                "readback": None,
            }
            for source in sources
        },
        "aggregate_gate": None,
        "validation": None,
        "error": None,
    }


def load_resume_manifest(
    manifest_path: Path,
    *,
    git_sha: str,
    sources: Sequence[str],
    page_cap: int,
    concurrency: int,
    transactions: Sequence[str] = TRANSACTIONS,
    database_target: Mapping[str, str] | None = None,
    max_age_hours: float = DEFAULT_MAX_RESUME_AGE_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    value = _load_json(manifest_path)
    if value.get("schema_version") != SCHEMA_VERSION:
        raise RefreshError("unsupported checkpoint manifest schema")
    if value.get("collector_git_sha") != git_sha:
        raise RefreshError("cannot resume with a different collector Git SHA")
    expected = {
        "sources": list(sources),
        "transactions": list(transactions),
        "max_items": 0,
        "page_cap": page_cap,
        "concurrency": concurrency,
        "additive": True,
        "status_activation": False,
        "mark_missing": False,
    }
    if value.get("config") != expected:
        raise RefreshError("resume configuration differs from the manifest")
    if database_target is not None:
        recorded_target = (value.get("preflight") or {}).get("database_target")
        if recorded_target != dict(database_target):
            raise RefreshError("cannot resume against a different database target")
    if not isinstance(value.get("sources"), dict):
        raise RefreshError("manifest sources checkpoint map is missing")
    if not math.isfinite(max_age_hours) or max_age_hours <= 0:
        raise RefreshError("maximum resume age must be finite and positive")
    started_at = parse_iso8601(
        value.get("started_at"),
        field="manifest.started_at",
    )
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = current - started_at
    if age < timedelta(0):
        raise RefreshError(
            "checkpoint generation starts in the future; "
            "start a new refresh generation instead of resuming"
        )
    if age > timedelta(hours=max_age_hours):
        raise RefreshError(
            f"checkpoint generation is older than {max_age_hours:g} hours; "
            "start a new refresh generation instead of resuming"
        )
    return value


def save_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = utc_now()
    atomic_write_json(run_dir / "manifest.json", manifest)


def manifest_database_target_sha256(
    manifest: Mapping[str, Any],
) -> str | None:
    """Return the bound target hash; test-only manifests may be intentionally unbound."""
    target = (manifest.get("preflight") or {}).get("database_target")
    if target is None:
        return None
    if (
        not isinstance(target, dict)
        or target.get("algorithm") != "sha256"
        or not isinstance(target.get("value"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", target["value"])
    ):
        raise GlobalStageError("checkpoint database target fingerprint is malformed")
    return target["value"]


def _relative_to_run(path: Path, run_dir: Path) -> str:
    return str(path.relative_to(run_dir))


def _archive_rejected(tmp_artifact: Path, run_dir: Path, source: str, attempt_number: int) -> str | None:
    if not tmp_artifact.exists():
        return None
    rejected = run_dir / "rejected" / f"{source}-attempt-{attempt_number}.json"
    rejected.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp_artifact, rejected)
    return _relative_to_run(rejected, run_dir)


def _checkpoint_artifact_valid(
    run_dir: Path,
    checkpoint: Mapping[str, Any],
    source: str,
    generation_started_at: str | datetime | None = None,
    *,
    transactions: Sequence[str] = TRANSACTIONS,
) -> tuple[Path, dict[str, Any]] | None:
    artifact_info = checkpoint.get("artifact")
    if not isinstance(artifact_info, dict):
        return None
    rel = artifact_info.get("path")
    expected_hash = artifact_info.get("sha256")
    if not isinstance(rel, str) or not isinstance(expected_hash, str):
        return None
    path = run_dir / rel
    if not path.is_file() or sha256_file(path) != expected_hash:
        return None
    attempt_started = artifact_info.get("attempt_started_at") or artifact_info.get("started_at")
    generation_bound = (
        source in STRICT_FRESHNESS_SOURCE_KEYS
        or source in PROPERTY_DETAIL_FRESHNESS_SOURCE_KEYS
    )
    try:
        stats = validate_source_artifact(
            path,
            source,
            attempt_started,
            require_strict_freshness=source in STRICT_FRESHNESS_SOURCE_KEYS,
            expected_generation_id=(
                run_dir.name if generation_bound else None
            ),
            expected_generation_started_at=(
                generation_started_at if generation_bound else None
            ),
            expected_transactions=transactions,
        )
    except ArtifactValidationError:
        return None
    return path, stats


def _manifest_checkpoint_artifact_valid(
    run_dir: Path,
    manifest: Mapping[str, Any],
    source: str,
) -> tuple[Path, dict[str, Any]] | None:
    checkpoint = manifest["sources"][source]
    transactions = tuple(manifest["config"]["transactions"])
    if transactions == TRANSACTIONS:
        return _checkpoint_artifact_valid(
            run_dir,
            checkpoint,
            source,
            manifest["started_at"],
        )
    return _checkpoint_artifact_valid(
        run_dir,
        checkpoint,
        source,
        manifest["started_at"],
        transactions=transactions,
    )


def collect_source(
    run_dir: Path,
    manifest: dict[str, Any],
    source: str,
    *,
    transactions: Sequence[str],
    page_cap: int,
    concurrency: int,
    attempts_this_run: int,
) -> tuple[Path, dict[str, Any]] | None:
    checkpoint = manifest["sources"][source]
    existing = _manifest_checkpoint_artifact_valid(run_dir, manifest, source)
    if existing:
        return existing

    canonical = run_dir / "sources" / f"{source}.json"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    tmp_artifact = canonical.with_suffix(".json.tmp")
    for _ in range(attempts_this_run):
        attempt_number = len(checkpoint["attempts"]) + 1
        attempt_started = utc_now()
        attempt_log = run_dir / "logs" / f"{source}-collect-attempt-{attempt_number}.log"
        tmp_artifact.unlink(missing_ok=True)
        env, overrides = fresh_source_env(
            source,
            run_dir,
            generation_started_at=manifest["started_at"],
            attempt_number=attempt_number,
        )
        attempt = {
            "number": attempt_number,
            "started_at": attempt_started,
            "finished_at": None,
            "rc": None,
            "log": _relative_to_run(attempt_log, run_dir),
            "freshness_overrides": {
                key: ("<run-local-path>" if str(run_dir) in value else value)
                for key, value in overrides.items()
            },
            "rejected_artifact": None,
            "error": None,
        }
        checkpoint["attempts"].append(attempt)
        checkpoint["state"] = "collecting"
        save_manifest(run_dir, manifest)
        rc = run_command(
            build_collect_argv(
                source,
                tmp_artifact,
                page_cap=page_cap,
                concurrency=concurrency,
                transactions=transactions,
            ),
            attempt_log,
            env=env,
        )
        attempt["rc"] = rc
        attempt["finished_at"] = utc_now()
        if rc != 0:
            attempt["error"] = f"collector exited {rc}"
            attempt["rejected_artifact"] = _archive_rejected(
                tmp_artifact, run_dir, source, attempt_number
            )
            checkpoint["state"] = "collect_failed"
            save_manifest(run_dir, manifest)
            continue
        try:
            generation_bound = (
                source in STRICT_FRESHNESS_SOURCE_KEYS
                or source in PROPERTY_DETAIL_FRESHNESS_SOURCE_KEYS
            )
            stats = validate_source_artifact(
                tmp_artifact,
                source,
                attempt_started,
                require_strict_freshness=source in STRICT_FRESHNESS_SOURCE_KEYS,
                expected_generation_id=(
                    run_dir.name if generation_bound else None
                ),
                expected_generation_started_at=(
                    manifest["started_at"] if generation_bound else None
                ),
                expected_transactions=transactions,
            )
        except ArtifactValidationError as exc:
            attempt["error"] = str(exc)
            attempt["rejected_artifact"] = _archive_rejected(
                tmp_artifact, run_dir, source, attempt_number
            )
            checkpoint["state"] = "artifact_rejected"
            save_manifest(run_dir, manifest)
            continue
        os.replace(tmp_artifact, canonical)
        stats["sha256"] = sha256_file(canonical)
        stats["bytes"] = canonical.stat().st_size
        checkpoint["artifact"] = {
            **stats,
            "path": _relative_to_run(canonical, run_dir),
            "attempt_started_at": attempt_started,
        }
        checkpoint["state"] = "validated"
        save_manifest(run_dir, manifest)
        return canonical, stats
    return None


def subset_gate_can_admit(info: Mapping[str, Any]) -> bool:
    """Allow only clean or baseline-only holds into additive subset mode."""
    verdict = info.get("verdict")
    reason = str(info.get("reason") or "")
    if verdict == "ok":
        return True
    if verdict != "hold" or not reason.startswith("current_active "):
        return False
    return " below floor " in reason or (
        " below " in reason
        and " of baseline median " in reason
        and " (threshold " in reason
    )


def gate_source(
    run_dir: Path,
    manifest: dict[str, Any],
    source: str,
    artifact: Path,
    env_file: str | None,
) -> None:
    checkpoint = manifest["sources"][source]
    full_transaction_scope = (
        tuple(manifest["config"]["transactions"]) == TRANSACTIONS
    )
    gate_path = run_dir / "gates" / f"{source}.json"
    log_path = run_dir / "logs" / f"{source}-gate.log"
    gate_path.parent.mkdir(parents=True, exist_ok=True)
    rc = run_command(
        build_gate_argv(
            artifact,
            gate_path,
            env_file,
            expected_db_target_sha256=manifest_database_target_sha256(manifest),
        ),
        log_path,
        env=safe_process_env(),
    )
    if rc not in (0, 2):
        checkpoint["state"] = "gate_failed"
        save_manifest(run_dir, manifest)
        raise GlobalStageError(f"coverage gate infrastructure failed for {source} (rc={rc})")
    try:
        result = _load_json(gate_path)
        if not full_transaction_scope:
            summary = result.get("summary")
            if (
                not isinstance(summary, dict)
                or _int_value(summary.get("torow_errors")) != 0
            ):
                raise GlobalStageError(
                    f"subset coverage gate reported conversion errors for {source}"
                )
            result["scope"] = {
                "kind": "additive_transaction_subset",
                "transactions": list(manifest["config"]["transactions"]),
                "whole_source_coverage": False,
            }
            scoped_info = result["per_source"][source]
            scoped_info["raw_verdict"] = scoped_info.get("verdict")
            scoped_info["raw_reason"] = scoped_info.get("reason")
            subset_admitted = subset_gate_can_admit(scoped_info)
            if subset_admitted:
                scoped_info["verdict"] = "ok_additive_subset"
                scoped_info["reason"] = (
                    "strict selected-transaction artifact admitted additively; "
                    "whole-source coverage baseline is advisory only"
                )
            scoped_info["mark_missing_safe"] = False
            scoped_info["admission_scope"] = (
                "additive_transaction_subset"
                if subset_admitted
                else "subset_admission_blocked"
            )
            summary["baseline_advisory_holds"] = (
                [source]
                if subset_admitted and scoped_info["raw_verdict"] == "hold"
                else []
            )
            summary["hold_sources"] = (
                []
                if subset_admitted
                else list(summary.get("hold_sources") or [])
            )
            summary["mark_missing_safe_brokerages"] = []
            atomic_write_json(gate_path, result)
        per_source = result["per_source"][source]
    except (ArtifactValidationError, KeyError, TypeError) as exc:
        checkpoint["state"] = "gate_failed"
        save_manifest(run_dir, manifest)
        raise GlobalStageError(f"coverage gate output is invalid for {source}") from exc
    checkpoint["gate"] = {
        "path": _relative_to_run(gate_path, run_dir),
        "log": _relative_to_run(log_path, run_dir),
        "rc": rc,
        "verdict": per_source.get("verdict"),
        "reason": per_source.get("reason"),
        "raw_verdict": per_source.get("raw_verdict"),
        "raw_reason": per_source.get("raw_reason"),
        "admission_scope": per_source.get("admission_scope"),
        # A transaction subset can prove additive freshness for the selected
        # rows, but can never authorize whole-source lifecycle deletion.
        "mark_missing_safe": (
            full_transaction_scope
            and per_source.get("mark_missing_safe") is True
        ),
        "transaction_scope": list(manifest["config"]["transactions"]),
    }
    checkpoint["state"] = "gated"
    save_manifest(run_dir, manifest)


def dry_run_source(
    run_dir: Path,
    manifest: dict[str, Any],
    source: str,
    artifact: Path,
) -> bool:
    checkpoint = manifest["sources"][source]
    sql_dir = run_dir / "dry-run" / source
    log_path = run_dir / "logs" / f"{source}-ingest-dry-run.log"
    shutil.rmtree(sql_dir, ignore_errors=True)
    rc = run_command(
        build_ingest_dry_run_argv(
            artifact,
            sql_dir,
            require_strict_freshness=source in STRICT_FRESHNESS_SOURCE_KEYS,
        ),
        log_path,
        env=safe_process_env(),
    )
    sql_path = sql_dir / "ingest.sql"
    sql_info = None
    if sql_path.is_file():
        sql_info = {"sha256": sha256_file(sql_path), "bytes": sql_path.stat().st_size}
        sql_path.unlink()
    checkpoint["dry_run"] = {
        "rc": rc,
        "log": _relative_to_run(log_path, run_dir),
        "sql": sql_info,
    }
    if rc != 0:
        checkpoint["state"] = "dry_run_failed"
        save_manifest(run_dir, manifest)
        return False
    checkpoint["state"] = "dry_run_passed"
    save_manifest(run_dir, manifest)
    return True


def ingest_source(
    run_dir: Path,
    manifest: dict[str, Any],
    source: str,
    artifact: Path,
    env_file: str | None,
) -> None:
    checkpoint = manifest["sources"][source]
    log_path = run_dir / "logs" / f"{source}-ingest.log"
    started = utc_now()
    argv = build_ingest_argv(
        artifact,
        env_file,
        require_strict_freshness=source in STRICT_FRESHNESS_SOURCE_KEYS,
        expected_db_target_sha256=manifest_database_target_sha256(manifest),
    )
    checkpoint["ingest"] = {
        "started_at": started,
        "finished_at": None,
        "rc": None,
        "log": _relative_to_run(log_path, run_dir),
        "additive": True,
        "status_activation": False,
        "mark_missing": False,
    }
    checkpoint["state"] = "ingesting"
    save_manifest(run_dir, manifest)
    rc = run_command(argv, log_path, env=safe_process_env())
    checkpoint["ingest"]["finished_at"] = utc_now()
    checkpoint["ingest"]["rc"] = rc
    if rc != 0:
        checkpoint["ingest_recovery"] = {
            "reason": "nonzero_live_ingest_result",
            "subprocess_rc": rc,
            "readback_ok": False,
        }
        save_manifest(run_dir, manifest)
        recover_interrupted_ingest(run_dir, manifest, source, env_file)
        return
    checkpoint["state"] = "ingested"
    save_manifest(run_dir, manifest)


def advance_source(
    run_dir: Path,
    manifest: dict[str, Any],
    source: str,
    *,
    page_cap: int,
    concurrency: int,
    attempts_this_run: int,
    env_file: str | None,
) -> bool:
    checkpoint = manifest["sources"][source]
    transactions = tuple(manifest["config"]["transactions"])
    existing = _manifest_checkpoint_artifact_valid(run_dir, manifest, source)
    if checkpoint.get("state") == "ingest_recovery_required":
        raise GlobalStageError(
            f"source {source} requires reviewed ingest recovery before resume"
        )
    if checkpoint.get("state") == "ingesting" and not existing:
        checkpoint["state"] = "ingest_recovery_required"
        checkpoint["ingest_recovery"] = {
            "readback_ok": False,
            "reason": "invalid_or_missing_artifact",
        }
        save_manifest(run_dir, manifest)
        raise GlobalStageError(
            f"source {source} has an ambiguous interrupted ingest with an "
            "invalid or missing artifact"
        )
    if checkpoint.get("state") == "ingesting":
        recover_interrupted_ingest(run_dir, manifest, source, env_file)
    if checkpoint.get("state") == "ingested" and existing:
        prior_verdict = (checkpoint.get("gate") or {}).get("verdict")
        admitted_verdict = (
            "ok"
            if transactions == TRANSACTIONS
            else "ok_additive_subset"
        )
        if prior_verdict == admitted_verdict:
            return True
        artifact, _stats = existing
        gate_source(run_dir, manifest, source, artifact, env_file)
        verdict = (checkpoint.get("gate") or {}).get("verdict")
        if verdict != admitted_verdict:
            checkpoint["state"] = "ingested"
            checkpoint["admission_state"] = (
                "baseline_seed_required"
                if verdict == "first_seen"
                else "gate_blocked"
            )
            save_manifest(run_dir, manifest)
            return False
        checkpoint["state"] = "ingested"
        checkpoint.pop("admission_state", None)
        save_manifest(run_dir, manifest)
        return True
    collected = existing or collect_source(
        run_dir,
        manifest,
        source,
        transactions=transactions,
        page_cap=page_cap,
        concurrency=concurrency,
        attempts_this_run=attempts_this_run,
    )
    if not collected:
        return False
    artifact, _stats = collected
    gate_source(run_dir, manifest, source, artifact, env_file)
    verdict = (checkpoint.get("gate") or {}).get("verdict")
    admitted_verdict = (
        "ok"
        if transactions == TRANSACTIONS
        else "ok_additive_subset"
    )
    if verdict != admitted_verdict:
        checkpoint["state"] = (
            "baseline_seed_required" if verdict == "first_seen" else "gate_blocked"
        )
        save_manifest(run_dir, manifest)
        return False
    if not dry_run_source(run_dir, manifest, source, artifact):
        return False
    return True


def prepare_sources(
    run_dir: Path,
    manifest: dict[str, Any],
    sources: Sequence[str],
    *,
    page_cap: int,
    concurrency: int,
    attempts_this_run: int,
    env_file: str | None,
) -> list[str]:
    """Prepare sources in order and stop at the first failed admission."""
    for source in sources:
        if not advance_source(
            run_dir,
            manifest,
            source,
            page_cap=page_cap,
            concurrency=concurrency,
            attempts_this_run=attempts_this_run,
            env_file=env_file,
        ):
            return [source]
    return []


def run_aggregate_gate(
    run_dir: Path,
    manifest: dict[str, Any],
    env_file: str | None,
) -> None:
    artifacts = [
        run_dir / manifest["sources"][source]["artifact"]["path"]
        for source in manifest["config"]["sources"]
    ]
    output = run_dir / "aggregate-gate.json"
    log = run_dir / "logs" / "aggregate-gate.log"
    argv = [sys.executable, "cre_gate.py"]
    for artifact in artifacts:
        argv.extend(["--in", str(artifact)])
    argv.extend(["--apply", "--strict", "--out", str(output)])
    if env_file:
        argv.extend(["--env-file", env_file])
    expected_target = manifest_database_target_sha256(manifest)
    if expected_target:
        argv.extend(["--expected-db-target-sha256", expected_target])
    rc = run_command(argv, log, env=safe_process_env())
    if rc not in (0, 2):
        raise GlobalStageError(f"aggregate coverage gate failed (rc={rc})")
    result = _load_json(output)
    full_transaction_scope = (
        tuple(manifest["config"]["transactions"]) == TRANSACTIONS
    )
    if not full_transaction_scope:
        summary = result.get("summary")
        if (
            not isinstance(summary, dict)
            or _int_value(summary.get("torow_errors")) != 0
        ):
            raise GlobalStageError(
                "subset aggregate coverage gate reported conversion errors"
            )
        result["scope"] = {
            "kind": "additive_transaction_subset",
            "transactions": list(manifest["config"]["transactions"]),
            "whole_source_coverage": False,
        }
        baseline_advisory_holds: list[str] = []
        for source, info in (result.get("per_source") or {}).items():
            if isinstance(info, dict):
                info["raw_verdict"] = info.get("verdict")
                info["raw_reason"] = info.get("reason")
                subset_admitted = subset_gate_can_admit(info)
                if subset_admitted:
                    info["verdict"] = "ok_additive_subset"
                    info["reason"] = (
                        "strict selected-transaction artifact admitted additively; "
                        "whole-source coverage baseline is advisory only"
                    )
                    if info["raw_verdict"] == "hold":
                        baseline_advisory_holds.append(source)
                info["mark_missing_safe"] = False
                info["admission_scope"] = (
                    "additive_transaction_subset"
                    if subset_admitted
                    else "subset_admission_blocked"
                )
        summary["baseline_advisory_holds"] = sorted(
            baseline_advisory_holds
        )
        summary["hold_sources"] = sorted(
            source
            for source, info in (result.get("per_source") or {}).items()
            if isinstance(info, dict) and info.get("verdict") == "hold"
        )
        summary["mark_missing_safe_brokerages"] = []
        atomic_write_json(output, result)
    per_source = result.get("per_source") or {}
    configured_sources = set(manifest["config"]["sources"])
    observed_sources = set(per_source) if isinstance(per_source, dict) else set()
    non_ok_sources = sorted(
        configured_sources - observed_sources
        | {
        source
        for source, info in per_source.items()
        if not isinstance(info, dict)
        or info.get("verdict")
        != ("ok" if full_transaction_scope else "ok_additive_subset")
        }
    )
    manifest["aggregate_gate"] = {
        "path": _relative_to_run(output, run_dir),
        "log": _relative_to_run(log, run_dir),
        "rc": rc,
        "hold_sources": (result.get("summary") or {}).get("hold_sources") or [],
        "baseline_advisory_holds": (
            (result.get("summary") or {}).get("baseline_advisory_holds") or []
        ),
        "non_ok_sources": non_ok_sources,
        "mark_missing_safe_brokerages": (
            (result.get("summary") or {}).get("mark_missing_safe_brokerages") or []
            if full_transaction_scope
            else []
        ),
        "transaction_scope": list(manifest["config"]["transactions"]),
    }
    save_manifest(run_dir, manifest)
    if non_ok_sources:
        raise RefreshError(
            "aggregate coverage gate is not established for source(s): "
            + ", ".join(non_ok_sources)
        )


def ingest_admitted_sources(
    run_dir: Path,
    manifest: dict[str, Any],
    env_file: str | None,
) -> None:
    """Ingest only after every configured source clears the aggregate gate."""
    for source in manifest["config"]["sources"]:
        checkpoint = manifest["sources"][source]
        existing = _manifest_checkpoint_artifact_valid(run_dir, manifest, source)
        if checkpoint.get("state") == "ingested" and existing:
            continue
        if checkpoint.get("state") == "ingesting" and existing:
            recover_interrupted_ingest(
                run_dir,
                manifest,
                source,
                env_file,
            )
            continue
        if checkpoint.get("state") != "dry_run_passed" or not existing:
            raise GlobalStageError(
                f"source {source} is not prepared for admitted ingest"
            )
        artifact, _stats = existing
        ingest_source(run_dir, manifest, source, artifact, env_file)


def recover_interrupted_ingest(
    run_dir: Path,
    manifest: dict[str, Any],
    source: str,
    env_file: str | None,
) -> None:
    """Resolve an interrupted live-ingest window without replaying writes."""
    checkpoint = manifest["sources"][source]
    output = run_dir / "recovery" / f"{source}-validation.json"
    log = run_dir / "logs" / f"{source}-ingest-recovery.log"
    output.parent.mkdir(parents=True, exist_ok=True)
    rc = run_command(
        build_validate_argv(
            output,
            env_file,
            expected_db_target_sha256=manifest_database_target_sha256(manifest),
        ),
        log,
        env=safe_process_env(),
    )
    recovery = {
        "validation_path": _relative_to_run(output, run_dir),
        "log": _relative_to_run(log, run_dir),
        "rc": rc,
        "readback_ok": False,
        "reason": (
            (checkpoint.get("ingest_recovery") or {}).get("reason")
            or "interrupted_live_ingest"
        ),
    }
    prior_recovery = checkpoint.get("ingest_recovery") or {}
    if prior_recovery.get("subprocess_rc") is not None:
        recovery["subprocess_rc"] = prior_recovery["subprocess_rc"]
    checkpoint["ingest_recovery"] = recovery
    if rc != 0:
        checkpoint["state"] = "ingest_recovery_required"
        save_manifest(run_dir, manifest)
        raise GlobalStageError(
            f"interrupted ingest readback failed for {source} (rc={rc})"
        )
    try:
        validation = _load_json(output)
        probe_manifest = {
            "config": {"sources": [source]},
            "sources": {
                source: {
                    "artifact": checkpoint.get("artifact"),
                }
            },
        }
        readback = verify_validation_readback(run_dir, probe_manifest, validation)
    except Exception as exc:
        checkpoint["state"] = "ingest_recovery_required"
        recovery["readback_error"] = str(exc)
        save_manifest(run_dir, manifest)
        raise GlobalStageError(
            f"interrupted ingest readback is invalid for {source}; "
            "manual recovery is required before replay"
        ) from exc
    checkpoint["readback"] = probe_manifest["sources"][source].get("readback")
    recovery["readback_ok"] = readback["ok"]
    if not readback["ok"]:
        checkpoint["state"] = "ingest_recovery_required"
        save_manifest(run_dir, manifest)
        raise GlobalStageError(
            f"interrupted ingest outcome is not exact for {source}; "
            "manual recovery is required before replay"
        )
    checkpoint["state"] = "ingested"
    checkpoint["ingest"]["recovered_from_exact_readback"] = True
    save_manifest(run_dir, manifest)


def _timestamp_second(value: str) -> datetime:
    return parse_iso8601(value, field="timestamp").replace(microsecond=0)


def verify_validation_readback(
    run_dir: Path,
    manifest: dict[str, Any],
    validation: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    latest_allowed = current + MAX_FUTURE_CLOCK_SKEW
    queries = validation.get("queries")
    rows = queries.get("source_counts") if isinstance(queries, dict) else None
    if not isinstance(rows, list):
        raise GlobalStageError("validation source_counts readback is missing")
    by_source = {
        row.get("source_key"): row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("source_key"), str)
    }
    generation_rows = (
        queries.get("freshness_generations")
        if isinstance(queries, dict)
        else None
    )
    generation_by_source = {
        (row.get("source_key"), row.get("generation_id")): row
        for row in (generation_rows or [])
        if isinstance(row, dict)
        and isinstance(row.get("source_key"), str)
        and isinstance(row.get("generation_id"), str)
    }
    inventory_rows = (
        queries.get("inventory_only_index") if isinstance(queries, dict) else None
    )
    if not isinstance(inventory_rows, list):
        raise GlobalStageError("validation inventory_only_index readback is missing")
    inventory_by_source = {
        row.get("source_key"): row
        for row in inventory_rows
        if isinstance(row, dict) and isinstance(row.get("source_key"), str)
    }
    failures: list[str] = []
    for source in manifest["config"]["sources"]:
        checkpoint = manifest["sources"][source]
        artifact = checkpoint.get("artifact") or {}
        try:
            staged = int(artifact["staged_unique"])
        except (KeyError, TypeError, ValueError, ArtifactValidationError):
            checkpoint["readback"] = {
                "ok": False,
                "reason": "malformed artifact readback expectation",
            }
            failures.append(source)
            continue
        row = by_source.get(source)
        generation_readback = (
            source in STRICT_FRESHNESS_SOURCE_KEYS
            or artifact.get("strict_freshness") is True
            or artifact.get("property_detail_freshness") is True
        )
        generation_id = None
        generation_started = None
        generation_row = None
        if generation_readback:
            try:
                generation_id = artifact["freshness_generation_id"]
                if not isinstance(generation_id, str) or not generation_id:
                    raise ValueError("missing generation id")
                generation_started = _timestamp_second(
                    artifact["freshness_generation_started_at"]
                )
            except (KeyError, TypeError, ValueError, ArtifactValidationError):
                checkpoint["readback"] = {
                    "ok": False,
                    "reason": "malformed generation readback expectation",
                }
                failures.append(source)
                continue
            if generation_started > latest_allowed:
                checkpoint["readback"] = {
                    "ok": False,
                    "generation_id": generation_id,
                    "reason": (
                        "generation start exceeds the 5-minute "
                        "clock-skew allowance"
                    ),
                }
                failures.append(source)
                continue
            generation_row = generation_by_source.get((source, generation_id))
            if generation_row is None:
                checkpoint["readback"] = {
                    "ok": False,
                    "generation_id": generation_id,
                    "generation_started_at": generation_started.isoformat(),
                    "reason": "generation is missing from validation readback",
                }
                failures.append(source)
                continue
            try:
                generation_count = int(generation_row["active"])
                earliest_inventory = _timestamp_second(
                    generation_row["earliest_inventory_observed_at"]
                )
                latest_inventory = _timestamp_second(
                    generation_row["latest_inventory_observed_at"]
                )
                earliest_detail = _timestamp_second(
                    generation_row["earliest_detail_scraped_at"]
                )
                latest_detail = _timestamp_second(
                    generation_row["latest_detail_scraped_at"]
                )
            except (KeyError, TypeError, ValueError, ArtifactValidationError):
                checkpoint["readback"] = {
                    "ok": False,
                    "generation_id": generation_id,
                    "reason": "malformed generation validation row",
                }
                failures.append(source)
                continue
            if any(
                timestamp > latest_allowed
                for timestamp in (
                    earliest_inventory,
                    latest_inventory,
                    earliest_detail,
                    latest_detail,
                )
            ):
                checkpoint["readback"] = {
                    "ok": False,
                    "generation_id": generation_id,
                    "reason": (
                        "generation readback observation exceeds the 5-minute "
                        "clock-skew allowance"
                    ),
                }
                failures.append(source)
                continue
            ok = (
                generation_count == staged
                and earliest_inventory >= generation_started
                and earliest_detail >= generation_started
            )
            reason = None
            if generation_count != staged:
                reason = (
                    f"generation batch {generation_count} != staged unique {staged}"
                )
            elif earliest_inventory < generation_started:
                reason = "generation inventory observation predates generation start"
            elif earliest_detail < generation_started:
                reason = "generation detail observation predates generation start"
            latest = latest_inventory
            latest_count = generation_count
            detail_latest_raw = generation_row["latest_detail_scraped_at"]
            detail_count = generation_count
            readback_boundary = generation_started
        else:
            try:
                expected = _timestamp_second(artifact["finished_at"])
            except (KeyError, TypeError, ValueError, ArtifactValidationError):
                checkpoint["readback"] = {
                    "ok": False,
                    "reason": "malformed artifact readback expectation",
                }
                failures.append(source)
                continue
            if expected > latest_allowed:
                checkpoint["readback"] = {
                    "ok": False,
                    "reason": (
                        "artifact readback boundary exceeds the 5-minute "
                        "clock-skew allowance"
                    ),
                }
                failures.append(source)
                continue
            readback_boundary = expected
            inventory_only_canonical_na = (
                staged == 0
                and source in INVENTORY_ONLY_SOURCE_DEFINITIONS
            )
            if row is None and not inventory_only_canonical_na:
                checkpoint["readback"] = {
                    "ok": False,
                    "reason": "source missing from validation",
                }
                failures.append(source)
                continue
            if inventory_only_canonical_na:
                latest = None
                latest_count = 0
                ok = True
                reason = None
            else:
                try:
                    latest = _timestamp_second(row["latest_inventory_observed_at"])
                    latest_count = int(row["latest_inventory_batch_active"])
                    latest_detail_value = row.get("latest_scraped_at")
                    latest_detail_timestamp = (
                        _timestamp_second(latest_detail_value)
                        if latest_detail_value
                        else None
                    )
                except (KeyError, TypeError, ValueError, ArtifactValidationError):
                    checkpoint["readback"] = {
                        "ok": False,
                        "reason": "malformed validation row",
                    }
                    failures.append(source)
                    continue
                ok = (
                    latest >= expected
                    and latest_count == staged
                    and latest <= latest_allowed
                    and (
                        latest_detail_timestamp is None
                        or latest_detail_timestamp <= latest_allowed
                    )
                )
                reason = None
                if (
                    latest > latest_allowed
                    or (
                        latest_detail_timestamp is not None
                        and latest_detail_timestamp > latest_allowed
                    )
                ):
                    reason = (
                        "validation readback observation exceeds the 5-minute "
                        "clock-skew allowance"
                    )
                elif latest < expected:
                    reason = (
                        f"latest inventory observation {latest.isoformat()} "
                        f"predates artifact {expected.isoformat()}"
                    )
                elif latest_count != staged:
                    reason = (
                        f"latest batch {latest_count} != staged unique {staged}"
                    )
            detail_latest_raw = (
                row.get("latest_scraped_at") if row is not None else None
            )
            detail_count = (
                row.get("latest_batch_active") if row is not None else None
            )
        expected_inventory_only = int(artifact.get("inventory_only") or 0)
        inventory_readback = inventory_by_source.get(source)
        inventory_ok = True
        inventory_reason = None
        inventory_details: dict[str, Any] = {
            "expected_active": expected_inventory_only,
        }
        if (
            expected_inventory_only
            or source in INVENTORY_ONLY_SOURCE_DEFINITIONS
        ):
            if inventory_readback is None:
                inventory_ok = False
                inventory_reason = "inventory-only source-index row is missing"
            else:
                try:
                    active_inventory = int(inventory_readback["active"])
                    latest_inventory_batch = int(
                        inventory_readback["latest_batch_active"]
                    )
                    latest_inventory_at_raw = inventory_readback.get(
                        "latest_enumerated_at"
                    )
                    latest_inventory_at = (
                        _timestamp_second(latest_inventory_at_raw)
                        if latest_inventory_at_raw
                        else None
                    )
                    scope_watermark_raw = inventory_readback.get(
                        "scope_watermark_at"
                    )
                    scope_watermark = (
                        _timestamp_second(scope_watermark_raw)
                        if scope_watermark_raw
                        else None
                    )
                except (KeyError, TypeError, ValueError, ArtifactValidationError):
                    inventory_ok = False
                    inventory_reason = "malformed inventory-only source-index row"
                else:
                    inventory_details.update(
                        {
                            "active": active_inventory,
                            "latest_batch_active": latest_inventory_batch,
                            "latest_enumerated_at": latest_inventory_at_raw,
                            "scope_watermark_at": scope_watermark_raw,
                            "soft_deleted": inventory_readback.get("soft_deleted"),
                        }
                    )
                    inventory_ok = (
                        active_inventory == expected_inventory_only
                        and latest_inventory_batch == expected_inventory_only
                        and (
                            expected_inventory_only == 0
                            or (
                                latest_inventory_at is not None
                                and latest_inventory_at >= readback_boundary
                            )
                        )
                        and scope_watermark is not None
                        and scope_watermark >= readback_boundary
                        and (
                            latest_inventory_at is None
                            or latest_inventory_at <= latest_allowed
                        )
                        and scope_watermark <= latest_allowed
                    )
                    if active_inventory != expected_inventory_only:
                        inventory_reason = (
                            f"active inventory-only {active_inventory} != "
                            f"expected {expected_inventory_only}"
                        )
                    elif latest_inventory_batch != expected_inventory_only:
                        inventory_reason = (
                            f"latest inventory-only batch {latest_inventory_batch} "
                            f"!= expected {expected_inventory_only}"
                        )
                    elif (
                        (
                            latest_inventory_at is not None
                            and latest_inventory_at > latest_allowed
                        )
                        or (
                            scope_watermark is not None
                            and scope_watermark > latest_allowed
                        )
                    ):
                        inventory_reason = (
                            "inventory-only readback observation exceeds "
                            "the 5-minute clock-skew allowance"
                        )
                    elif expected_inventory_only and (
                        latest_inventory_at is None
                        or latest_inventory_at < readback_boundary
                    ):
                        inventory_reason = (
                            "inventory-only latest enumeration predates artifact"
                        )
                    elif (
                        scope_watermark is None
                        or scope_watermark < readback_boundary
                    ):
                        inventory_reason = (
                            "inventory-only scope watermark predates artifact"
                        )
        inventory_details["ok"] = inventory_ok
        inventory_details["reason"] = inventory_reason
        ok = ok and inventory_ok
        if reason is None and inventory_reason is not None:
            reason = inventory_reason
        checkpoint["readback"] = {
            "ok": ok,
            "generation_id": generation_id,
            "generation_started_at": (
                generation_started.isoformat()
                if generation_started is not None
                else None
            ),
            "earliest_inventory_observed_at": (
                generation_row.get("earliest_inventory_observed_at")
                if generation_row is not None
                else None
            ),
            "latest_inventory_observed_at": (
                generation_row.get("latest_inventory_observed_at")
                if generation_row is not None
                else (row["latest_inventory_observed_at"] if row is not None else None)
            ),
            "latest_inventory_batch_active": latest_count,
            "earliest_detail_scraped_at": (
                generation_row.get("earliest_detail_scraped_at")
                if generation_row is not None
                else None
            ),
            "latest_detail_scraped_at": detail_latest_raw,
            "latest_detail_batch_active": detail_count,
            "detail_unavailable": (
                row.get("detail_unavailable") if row is not None else None
            ),
            "expected_staged_unique": staged,
            "inventory_only": inventory_details,
            "reason": reason,
        }
        if not ok:
            failures.append(source)
    save_manifest(run_dir, manifest)
    return {"ok": not failures, "failed_sources": failures}


def run_final_validation(
    run_dir: Path,
    manifest: dict[str, Any],
    env_file: str | None,
) -> None:
    output = run_dir / "validation.json"
    log = run_dir / "logs" / "validation.log"
    rc = run_command(
        build_validate_argv(
            output,
            env_file,
            expected_db_target_sha256=manifest_database_target_sha256(manifest),
        ),
        log,
        env=safe_process_env(),
    )
    if rc != 0:
        raise GlobalStageError(f"final validation failed (rc={rc})")
    result = _load_json(output)
    before_rel = (manifest.get("preflight") or {}).get("validation_path")
    if not isinstance(before_rel, str):
        raise GlobalStageError("pre-refresh validation snapshot is missing")
    before = _load_json(run_dir / before_rel)
    quality = compare_validation_quality(before, result)
    readback = verify_validation_readback(run_dir, manifest, result)
    manifest["validation"] = {
        "path": _relative_to_run(output, run_dir),
        "log": _relative_to_run(log, run_dir),
        "rc": rc,
        "query_execution_ok": result.get("ok") is True,
        "quality_no_regression": quality["ok"],
        "quality_failures": quality["failures"],
        "readback_ok": readback["ok"],
        "failed_readback_sources": readback["failed_sources"],
    }
    save_manifest(run_dir, manifest)
    if result.get("ok") is not True or not quality["ok"] or not readback["ok"]:
        raise GlobalStageError("final validation or per-source freshness readback failed")


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _rows_by(rows: Any, keys: Sequence[str]) -> dict[tuple[str, ...], Mapping[str, Any]]:
    if not isinstance(rows, list):
        return {}
    return {
        tuple(str(row.get(key) or "") for key in keys): row
        for row in rows
        if isinstance(row, dict)
    }


def compare_validation_quality(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    """Reject newly introduced hard defects or severe child-data loss."""
    before_queries = before.get("queries") if isinstance(before.get("queries"), dict) else {}
    after_queries = after.get("queries") if isinstance(after.get("queries"), dict) else {}
    failures: list[str] = []

    count_specs = (
        ("duplicates", ("check_name", "source_key"), ("groups", "rows")),
        ("bad_child_urls", ("check_name",), ("count",)),
        ("primary_child_conflicts", ("child_type",), ("listings",)),
        ("orphans", ("child_type",), ("orphan_rows",)),
        (
            "quality_by_source",
            ("source_key",),
            (
                "bad_source_url",
                "invalid_state",
                "impossible_lat",
                "impossible_lng",
                "sale_psf_flags",
                "lease_rate_flags",
                "cap_rate_flags",
            ),
        ),
    )
    for query, keys, fields in count_specs:
        old = _rows_by(before_queries.get(query), keys)
        new = _rows_by(after_queries.get(query), keys)
        for identity, row in new.items():
            prior = old.get(identity, {})
            for field in fields:
                old_value = _int_value(prior.get(field))
                new_value = _int_value(row.get(field))
                if new_value > old_value:
                    failures.append(
                        f"{query}/{identity}/{field} increased {old_value}->{new_value}"
                    )

    old_children = _rows_by(
        before_queries.get("child_counts"), ("source_key", "child_type")
    )
    new_children = _rows_by(
        after_queries.get("child_counts"), ("source_key", "child_type")
    )
    for identity, prior in old_children.items():
        old_count = _int_value(prior.get("count"))
        new_count = _int_value(new_children.get(identity, {}).get("count"))
        if old_count >= 10 and new_count < int(old_count * 0.7):
            failures.append(
                f"child_counts/{identity} fell more than 30%: {old_count}->{new_count}"
            )

    for row in after_queries.get("search_smoke") or []:
        if isinstance(row, dict) and _int_value(row.get("rows")) <= 0:
            failures.append(f"search_smoke/{row.get('smoke')} returned zero rows")
    return {"ok": not failures, "failures": failures}


def record_scope_from_validation(
    manifest: dict[str, Any], validation: Mapping[str, Any]
) -> None:
    rows = ((validation.get("queries") or {}).get("source_counts") or [])
    supported = set(manifest["config"]["sources"])
    unsupported = sum(
        _int_value(row.get("active"))
        for row in rows
        if isinstance(row, dict) and row.get("source_key") not in supported
    )
    manifest["scope"]["unsupported_active_rows_before"] = unsupported


def render_report(manifest: Mapping[str, Any]) -> str:
    lines = [
        "# CRE checkpoint refresh report",
        "",
        f"- Run: `{manifest.get('run_id')}`",
        f"- Status: `{manifest.get('status')}`",
        f"- Started: `{manifest.get('started_at')}`",
        f"- Finished: `{manifest.get('finished_at') or ''}`",
        f"- Collector SHA: `{manifest.get('collector_git_sha')}`",
        f"- Transaction scope: `{(manifest.get('config') or {}).get('transactions')}`",
        "- Write mode: additive only; status activation and mark-missing disabled.",
        (
            "- Scope admission: whole-source coverage."
            if tuple((manifest.get("config") or {}).get("transactions") or ())
            == TRANSACTIONS
            else "- Scope admission: additive transaction subset only; no whole-source coverage or lifecycle claim."
        ),
        "- Readback is source-class-aware: strict detail sources require current admitted detail observation; authoritative inventory feeds follow their explicit replace-or-preserve child contract; scoped sources never make a broader detail or contact freshness claim.",
        "",
        "| Source | State | Flat | Staged | Inventory-only | Detail unavailable | Provisional IDs | Gate | Readback |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    sources = manifest.get("sources") or {}
    for source in (manifest.get("config") or {}).get("sources") or []:
        checkpoint = sources.get(source) or {}
        artifact = checkpoint.get("artifact") or {}
        gate = checkpoint.get("gate") or {}
        readback = checkpoint.get("readback") or {}
        lines.append(
            f"| {source} | {checkpoint.get('state', '')} | "
            f"{artifact.get('flat_listings', '')} | {artifact.get('staged_unique', '')} | "
            f"{artifact.get('inventory_only', '')} | "
            f"{artifact.get('detail_unavailable', '')} | "
            f"{artifact.get('provisional_identities', '')} | "
            f"{gate.get('verdict', '')} | "
            f"{'ok' if readback.get('ok') is True else (readback.get('reason') or '')} |"
        )
    aggregate = manifest.get("aggregate_gate") or {}
    validation = manifest.get("validation") or {}
    lines.extend(
        [
            "",
            "## Final gates",
            "",
            f"- Coverage holds: `{aggregate.get('hold_sources', [])}`",
            f"- Validation query execution: `{validation.get('query_execution_ok')}`",
            f"- Validation quality regression check: `{validation.get('quality_no_regression')}`",
            f"- Per-source ingest readback: `{validation.get('readback_ok')}`",
            f"- Unsupported active rows outside this run: "
            f"`{(manifest.get('scope') or {}).get('unsupported_active_rows_before')}`",
            "",
        ]
    )
    if manifest.get("error"):
        lines.extend(["## Error", "", str(manifest["error"]), ""])
    return "\n".join(lines)


def parse_sources(raw: str) -> tuple[str, ...]:
    if raw.strip().lower() == "all":
        return SOURCE_KEYS
    values = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    unknown = [source for source in values if source not in SOURCE_KEYS]
    if not values or unknown or len(values) != len(set(values)):
        raise ValueError(f"invalid source selection; unknown/duplicate values: {unknown or values}")
    return values


def parse_transactions(raw: str) -> tuple[str, ...]:
    normalized = raw.strip().lower()
    if normalized in {"both", "all", "sale,lease"}:
        return TRANSACTIONS
    if normalized in TRANSACTIONS:
        return (normalized,)
    raise ValueError("invalid transaction selection; use sale, lease, or both")


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", default=None, help="existing run directory or manifest.json")
    parser.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT))
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--sources", default="all")
    parser.add_argument(
        "--transactions",
        default="both",
        help=(
            "transaction scope: sale, lease, or both (default). A subset run "
            "is additive and proves only the selected transaction scope."
        ),
    )
    parser.add_argument("--page-cap", type=int, default=400)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--attempts-per-source", type=int, default=3)
    parser.add_argument(
        "--max-resume-age-hours",
        type=float,
        default=DEFAULT_MAX_RESUME_AGE_HOURS,
        help=(
            "reject resumed refresh generations older than this many hours "
            f"(default: {DEFAULT_MAX_RESUME_AGE_HOURS:g})"
        ),
    )
    parser.add_argument(
        "--lock-dir",
        default=None,
        help="shared CRE lock path; defaults to the primary checkout lock even from a worktree",
    )
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args(argv)

    if args.page_cap < 1 or not 1 <= args.concurrency <= 6:
        parser.error("page-cap must be positive and concurrency must be between 1 and 6")
    if args.attempts_per_source < 1:
        parser.error("attempts-per-source must be positive")
    if (
        not math.isfinite(args.max_resume_age_hours)
        or args.max_resume_age_hours <= 0
    ):
        parser.error("max-resume-age-hours must be finite and positive")
    try:
        sources = parse_sources(args.sources)
        transactions = parse_transactions(args.transactions)
    except ValueError as exc:
        parser.error(str(exc))

    database_target = database_target_fingerprint(args.env_file)
    git_sha, git_dirty = git_identity()
    if git_dirty and not args.allow_dirty:
        raise RefreshError("refusing operational refresh from a dirty checkout")
    if args.resume:
        supplied = Path(args.resume).expanduser().resolve()
        manifest_path = supplied if supplied.name == "manifest.json" else supplied / "manifest.json"
        run_dir = manifest_path.parent
        manifest = load_resume_manifest(
            manifest_path,
            git_sha=git_sha,
            sources=sources,
            page_cap=args.page_cap,
            concurrency=args.concurrency,
            transactions=transactions,
            database_target=database_target,
            max_age_hours=args.max_resume_age_hours,
        )
        manifest["status"] = "running"
        manifest["error"] = None
    else:
        run_dir = Path(args.out_root).expanduser().resolve() / _run_id()
        if run_dir.exists():
            raise RefreshError(f"run directory already exists: {run_dir}")
        run_dir.mkdir(parents=True)
        manifest = new_manifest(
            run_dir,
            git_sha=git_sha,
            git_dirty=git_dirty,
            sources=sources,
            page_cap=args.page_cap,
            concurrency=args.concurrency,
            transactions=transactions,
            database_target=database_target,
        )
    save_manifest(run_dir, manifest)

    lock_dir = (
        Path(args.lock_dir).expanduser().resolve()
        if args.lock_dir
        else canonical_shared_lock_dir()
    )
    lock = SharedLock(lock_dir)
    try:
        with lock:
            health_log = run_dir / "logs" / "healthcheck.log"
            health_rc = run_command(
                ["bash", str(REPO_ROOT / "scripts/firecrawl-ops/firecrawl_healthcheck.sh")],
                health_log,
                env=safe_process_env(),
            )
            manifest["preflight"].update(
                {
                    "healthcheck_rc": health_rc,
                    "healthcheck_log": _relative_to_run(health_log, run_dir),
                    "shared_lock": str(lock_dir),
                }
            )
            save_manifest(run_dir, manifest)
            if health_rc != 0:
                raise GlobalStageError(f"Firecrawl healthcheck failed (rc={health_rc})")

            pre_validation = run_dir / "pre-validation.json"
            pre_validation_log = run_dir / "logs" / "pre-validation.log"
            recorded_pre_hash = manifest["preflight"].get("validation_sha256")
            if recorded_pre_hash:
                if (
                    not pre_validation.is_file()
                    or sha256_file(pre_validation) != recorded_pre_hash
                ):
                    raise GlobalStageError("pre-refresh validation snapshot changed")
                pre_result = _load_json(pre_validation)
            else:
                pre_rc = run_command(
                    build_validate_argv(
                        pre_validation,
                        args.env_file,
                        expected_db_target_sha256=(
                            manifest_database_target_sha256(manifest)
                        ),
                    ),
                    pre_validation_log,
                    env=safe_process_env(),
                )
                if pre_rc != 0:
                    raise GlobalStageError(f"pre-refresh validation failed (rc={pre_rc})")
                pre_result = _load_json(pre_validation)
                manifest["preflight"].update(
                    {
                        "validation_rc": pre_rc,
                        "validation_path": _relative_to_run(pre_validation, run_dir),
                        "validation_log": _relative_to_run(pre_validation_log, run_dir),
                        "validation_sha256": sha256_file(pre_validation),
                    }
                )
            record_scope_from_validation(manifest, pre_result)
            save_manifest(run_dir, manifest)

            source_failures = prepare_sources(
                run_dir,
                manifest,
                sources,
                page_cap=args.page_cap,
                concurrency=args.concurrency,
                attempts_this_run=args.attempts_per_source,
                env_file=args.env_file,
            )
            if source_failures:
                raise RefreshError(
                    "source checkpoints remain incomplete: " + ", ".join(source_failures)
                )
            run_aggregate_gate(run_dir, manifest, args.env_file)
            ingest_admitted_sources(run_dir, manifest, args.env_file)
            run_final_validation(run_dir, manifest, args.env_file)
            manifest["status"] = (
                "supported_scope_complete"
                if transactions == TRANSACTIONS
                else "selected_transaction_scope_complete"
            )
            manifest["finished_at"] = utc_now()
            manifest["error"] = None
            save_manifest(run_dir, manifest)
            atomic_write_text(run_dir / "report.md", render_report(manifest))
            print(run_dir)
            return 0
    except KeyboardInterrupt:
        manifest["status"] = "interrupted"
        manifest["error"] = "operator interruption"
        save_manifest(run_dir, manifest)
        atomic_write_text(run_dir / "report.md", render_report(manifest))
        return 130
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
        save_manifest(run_dir, manifest)
        atomic_write_text(run_dir / "report.md", render_report(manifest))
        raise


if __name__ == "__main__":
    raise SystemExit(main())
