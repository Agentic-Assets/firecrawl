"""Read settled, owner-bound series evidence for a small Windows Slack summary.

No database, provider, credential, lock mutation or recovery is performed. An
observed stdout path is only a locator; unavailable proof yields no counts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

import cre_stop_guarded_tier as safe

COLLECTOR = Path(__file__).resolve().parent
SERIES_ID = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z")
RUN_ID = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z-[0-9a-f]{12}")
HEX_ID = re.compile(r"[0-9a-f]{32}")
MAX_TRANSACTION_ARTIFACT_BYTES = 16 * 1024 * 1024
TRANSACTION_BREAKDOWN_BUDGET_SECONDS = 8.0
PIPELINE_SOURCE_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
MAX_VALIDATION_REPORT_BYTES = 4 * 1024 * 1024
MAX_TOTAL_VALIDATION_REPORT_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BINDING_BYTES = (1 << 63) - 1


class SummaryUnavailable(ValueError):
    """Evidence cannot support the requested notification metric."""


def require(condition):
    if not condition:
        raise SummaryUnavailable("verified summary unavailable")


def _pipeline_admissions(config, selected_sources):
    """Validate the finite admitted subset without requiring every source admitted."""
    admissions = config.get("current_inventory_admissions")
    require(
        isinstance(admissions, dict)
        and set(admissions) <= set(selected_sources)
        and all(
            isinstance(source, str) and PIPELINE_SOURCE_KEY.fullmatch(source)
            for source in admissions
        )
    )
    try:
        from cre_current_inventory_admission import configuration

        require(
            not admissions
            or (
                config.get("transactions") == ["sale", "lease"]
                and not config.get("admit_baseline_hold_additively", False)
                and not config.get("allow_dirty", False)
            )
        )
        require(
            all(value == configuration(source) for source, value in admissions.items())
        )
    except (ImportError, ValueError, KeyError, TypeError, OSError, AttributeError):
        raise SummaryUnavailable("pipeline admission map unavailable") from None
    return admissions


def timestamp(value):
    require(isinstance(value, str))
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.utcoffset() is not None)
    return result.timestamp()


def _derive_transaction_breakdown(data, source, expected_count, scraped_at, deadline):
    """Rebuild staged identity modes with the production ingester's functions."""
    from cre_ingest import (
        SOURCE_TO_BROKERAGE,
        merge_rows,
        to_row,
        validate_duplicate_identity_before_merge,
    )

    if (
        not isinstance(data, dict)
        or not isinstance(data.get("runMeta"), dict)
        or data["runMeta"].get("mode") != "full"
        or data["runMeta"].get("transactions") != ["sale", "lease"]
        or not isinstance(data.get("listings"), list)
        or len(data["listings"]) > 500000
        or not isinstance(data.get("brokers", []), list)
        or source not in SOURCE_TO_BROKERAGE
    ):
        raise SummaryUnavailable("transaction breakdown unavailable")

    merged = {}
    brokers_by_idx = {
        index: broker for index, broker in enumerate(data.get("brokers", []))
    }
    for index, listing in enumerate(data["listings"]):
        if index % 256 == 0 and time.monotonic() > deadline:
            raise SummaryUnavailable("transaction breakdown deadline exceeded")
        if not isinstance(listing, dict) or listing.get("sourceKey") != source:
            raise SummaryUnavailable("transaction artifact source mismatch")
        if listing.get("transactionMode") not in {"sale", "lease"}:
            raise SummaryUnavailable("transaction artifact mode is unsupported")
        row = to_row(listing, brokers_by_idx, scraped_at)
        if row is None:
            continue
        key = (row["slug"], row["external_id"])
        prior = merged.get(key)
        if prior is None:
            merged[key] = row
            continue
        validate_duplicate_identity_before_merge(prior, row)
        merged[key] = merge_rows(prior, row)

    if len(merged) != expected_count:
        raise SummaryUnavailable("transaction staged identity count mismatch")
    sale_ids = set()
    lease_ids = set()
    for identity, row in merged.items():
        modes = row.get("_modes")
        if not isinstance(modes, set) or not modes or not modes <= {"sale", "lease"}:
            raise SummaryUnavailable("transaction staged modes are unsupported")
        transaction_type = row.get("transaction_type")
        if transaction_type == "sale":
            sale_ids.add(identity)
        elif transaction_type == "lease":
            lease_ids.add(identity)
        elif transaction_type == "sale_or_lease":
            sale_ids.add(identity)
            lease_ids.add(identity)
        else:
            raise SummaryUnavailable("transaction staged type is unsupported")
    both_ids = sale_ids & lease_ids
    if len(sale_ids | lease_ids) != expected_count:
        raise SummaryUnavailable("transaction mode identity union mismatch")
    return {
        "sale": len(sale_ids),
        "lease": len(lease_ids),
        "both": len(both_ids),
        "measurement": "current_staged_inventory_transaction_types",
    }


def _staged_transaction_breakdown(artifact, source, expected_count, generation_id):
    """Validate producer-computed transaction counts bound to this artifact."""
    breakdown = artifact.get("transaction_breakdown")
    if breakdown is None:
        return None
    require(
        isinstance(breakdown, dict)
        and set(breakdown)
        == {
            "sale",
            "lease",
            "both",
            "measurement",
            "source_key",
            "artifact_sha256",
            "artifact_bytes",
            "generation_id",
        }
    )
    artifact_sha = artifact.get("sha256")
    artifact_bytes = artifact.get("bytes")
    require(
        isinstance(artifact_sha, str)
        and re.fullmatch(r"[0-9a-f]{64}", artifact_sha)
        and type(artifact_bytes) is int
        and 0 < artifact_bytes <= MAX_ARTIFACT_BINDING_BYTES
    )
    require(
        artifact.get("freshness_generation_id") == generation_id
        and breakdown.get("source_key") == source
        and breakdown.get("artifact_sha256") == artifact_sha
        and breakdown.get("artifact_bytes") == artifact_bytes
        and breakdown.get("generation_id") == generation_id
        and breakdown.get("measurement")
        == "current_staged_inventory_transaction_types"
    )
    require(
        0 <= time.time() - timestamp(artifact.get("finished_at")) <= 86400
    )
    sale, lease, both = (
        breakdown.get("sale"),
        breakdown.get("lease"),
        breakdown.get("both"),
    )
    require(
        type(sale) is int
        and type(lease) is int
        and type(both) is int
        and 0 <= sale <= expected_count
        and 0 <= lease <= expected_count
        and 0 <= both <= min(sale, lease)
        and sale + lease - both == expected_count
    )
    return {
        "sale": sale,
        "lease": lease,
        "both": both,
        "measurement": "current_staged_inventory_transaction_types",
    }


def _breakdown_status(breakdown, basis, reason, started):
    return {
        "status": "verified" if breakdown is not None else "unavailable",
        "basis": basis,
        "reason": reason,
        "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
    }


def verified_summary(series_directory, source, expected_sha, windows_admission_ms):
    """Return only a completed single-source generation; never infer success."""
    summary_started = time.monotonic()
    require(
        isinstance(source, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", source)
    )
    require(
        isinstance(expected_sha, str) and re.fullmatch(r"[0-9a-f]{40}", expected_sha)
    )
    require(type(windows_admission_ms) is int and windows_admission_ms > 0)
    selected = Path(series_directory)
    require(selected.parent == COLLECTOR / "out/checkpoint-series")
    require(SERIES_ID.fullmatch(selected.name))
    snapshots = []
    with ExitStack() as stack:

        def directory(parent, name):
            descriptor = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            stack.callback(os.close, descriptor)
            safe._safe_stat(descriptor, directory=True)
            return descriptor

        def read(parent, name, limit=1048576):
            raw, _ = safe._read_at(parent, name, limit)
            value = json.loads(raw)
            require(isinstance(value, dict))
            snapshots.append((parent, name, limit, raw))
            return value

        root = directory(None, str(COLLECTOR))
        output = directory(root, "out")
        series_root = directory(output, "checkpoint-series")
        series_fd = directory(series_root, selected.name)
        parent = read(series_fd, "manifest.json")
        require(
            type(parent.get("schema_version")) is int
            and parent["schema_version"] == 1
            and parent.get("series_id") == selected.name
        )
        require(parent.get("status") == "complete" and parent.get("error") is None)
        require(parent.get("collector_git_sha") == expected_sha)
        require(parent.get("collector_git_dirty") is False)
        config = parent["config"]
        require(
            config.get("sources") == [source]
            and config.get("transactions") == ["sale", "lease"]
        )
        admission = parent["latest_invocation_admission"]
        require(admission.get("expected_sha") == expected_sha)
        require(admission.get("windows_admission_unix_ms") == windows_admission_ms)
        invocation = parent["latest_invocation_control"]["invocation_id"]
        require(isinstance(invocation, str) and HEX_ID.fullmatch(invocation))
        require(
            parent["latest_invocation_control"].get("receipt")
            == f".series-control/{invocation}.json"
        )
        private = directory(series_fd, ".series-control")
        safe._safe_stat(private, directory=True, private=True)
        control = read(private, invocation + ".json", 16384)
        owner = control["stop_owner"]
        require(
            type(owner.get("schema_version")) is int and owner["schema_version"] == 1
        )
        require(owner.get("uid") == os.geteuid())
        require(owner.get("owner_role") == "checkpoint_series_one_shot_cli")
        require(owner.get("invocation_id") == invocation)
        require(
            owner.get("series_directory_identity")
            == safe._identity(os.fstat(series_fd))
        )
        require(
            owner.get("control_directory_identity") == safe._identity(os.fstat(private))
        )
        require(
            control.get("status") == "complete"
            and control.get("local_settlement") == "confirmed"
        )
        started, finished = control["started_unix_ms"], control["finished_unix_ms"]
        require(type(started) is int and type(finished) is int)
        require(
            windows_admission_ms
            <= started
            <= finished
            <= int(time.time() * 1000) + 60000
        )
        require(0 <= int(time.time() * 1000) - finished <= 300000)
        parent_start, parent_finish = (
            timestamp(parent["started_at"]),
            timestamp(parent["finished_at"]),
        )
        require(parent_start <= started / 1000 <= parent_finish <= finished / 1000)
        selected_source = parent["sources"][source]
        require(
            set(parent["sources"]) == {source}
            and selected_source.get("state") == "complete"
        )
        attempt = selected_source["attempts"][-1]
        require(type(attempt.get("rc")) is int and attempt["rc"] == 0)
        child_id = attempt["series_control_child_id"]
        require(isinstance(child_id, str) and HEX_ID.fullmatch(child_id))
        run_relative = selected_source["checkpoint_run"]
        require(isinstance(run_relative, str) and run_relative.startswith("runs/"))
        run_id = run_relative[5:]
        require(RUN_ID.fullmatch(run_id))
        require(control.get("last_checkpoint", {}).get("run_id") == run_id)
        require(control["last_checkpoint"].get("child_invocation_id") == child_id)
        runs = directory(series_fd, "runs")
        child_fd = directory(runs, run_id)
        child = read(child_fd, "manifest.json")
        require(
            type(child.get("schema_version")) is int
            and child["schema_version"] == 2
            and child.get("run_id") == run_id
        )
        require(
            child.get("collector_git_sha") == expected_sha
            and child.get("collector_git_dirty") is False
        )
        require(
            child.get("error") is None
            and selected_source.get("checkpoint_status") == child.get("status")
        )
        from cre_checkpoint_series import _child_succeeded

        require(_child_succeeded(child, config))
        require(
            parent_start
            <= timestamp(child["started_at"])
            <= timestamp(child["finished_at"])
            <= parent_finish
        )
        child_private = directory(child_fd, ".series-control")
        safe._safe_stat(child_private, directory=True, private=True)
        settlement = read(child_private, child_id + ".json", 16384)
        require(
            type(settlement.get("schema_version")) is int
            and settlement["schema_version"] == 1
        )
        require(
            settlement.get("invocation_id") == invocation
            and settlement.get("child_invocation_id") == child_id
        )
        require(
            settlement.get("run_id") == run_id
            and settlement.get("collector_git_sha") == expected_sha
        )
        require(
            type(settlement.get("pid")) is int
            and settlement["pid"] > 1
            and settlement["pid"] == control["last_checkpoint"].get("pid")
        )
        require(
            all(
                settlement.get(key) is True
                for key in (
                    "guards_stopped",
                    "nested_cleanup_confirmed",
                    "terminal_evidence_saved",
                )
            )
        )
        require(child.get("config", {}).get("sources") == [source])
        require(child["config"].get("transactions") == ["sale", "lease"])
        require(
            child.get("preflight", {}).get("database_target")
            == parent.get("database_target")
            and bool(parent.get("database_target"))
        )
        validation = child["validation"]
        require(type(validation.get("rc")) is int and validation["rc"] == 0)
        require(
            all(
                validation.get(key) is True
                for key in (
                    "query_execution_ok",
                    "quality_no_regression",
                    "readback_ok",
                )
            )
        )
        require(validation.get("failed_readback_sources") == [])
        checkpoint = child["sources"][source]
        require(
            set(child["sources"]) == {source} and checkpoint.get("state") == "ingested"
        )
        require(
            type(checkpoint.get("ingest", {}).get("rc")) is int
            and checkpoint["ingest"]["rc"] == 0
        )
        readback, artifact = checkpoint["readback"], checkpoint["artifact"]
        count = readback["expected_staged_unique"]
        require(type(count) is int and 0 < count <= 500000)
        require(
            readback.get("ok") is True
            and readback.get("latest_inventory_batch_active") == count
        )
        require(
            artifact.get("staged_unique") == count
            and artifact.get("inventory_only") == 0
        )
        require(artifact.get("rejected_by_ingest") == 0)
        require(
            isinstance(readback.get("generation_id"), str)
            and readback["generation_id"] == run_id
        )
        transaction_started = time.monotonic()
        transaction_breakdown = None
        breakdown_basis = "none"
        breakdown_reason = "staging_stats_absent"
        staged_stats = artifact.get("transaction_breakdown")
        if staged_stats is not None:
            try:
                transaction_breakdown = _staged_transaction_breakdown(
                    artifact, source, count, run_id
                )
                breakdown_basis = "artifact_staging_statistics"
                breakdown_reason = None
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                IndexError,
                AttributeError,
                safe.StopRefused,
            ):
                breakdown_reason = "staging_stats_invalid"
        else:
            try:
                artifact_path = artifact.get("path")
                artifact_sha = artifact.get("sha256")
                artifact_bytes = artifact.get("bytes")
                require(artifact_path == f"sources/{source}.json")
                require(
                    isinstance(artifact_sha, str)
                    and re.fullmatch(r"[0-9a-f]{64}", artifact_sha)
                )
                require(
                    type(artifact_bytes) is int
                    and 0 < artifact_bytes <= MAX_TRANSACTION_ARTIFACT_BYTES
                )
                sources_fd = directory(child_fd, "sources")
                raw, observed = safe._read_at(
                    sources_fd,
                    f"{source}.json",
                    limit=MAX_TRANSACTION_ARTIFACT_BYTES,
                )
                snapshots.append(
                    (sources_fd, f"{source}.json", MAX_TRANSACTION_ARTIFACT_BYTES, raw)
                )
                require(observed.st_size == artifact_bytes)
                require(hashlib.sha256(raw).hexdigest() == artifact_sha)
                data = json.loads(raw)
                freshness = data.get("runMeta", {}).get("freshness")
                require(
                    isinstance(freshness, dict)
                    and freshness.get("generationId") == run_id
                    and readback["generation_id"] == freshness["generationId"]
                    and 0
                    <= time.time() - timestamp(data["runMeta"]["finishedAt"])
                    <= 86400
                )
                require(
                    time.monotonic() - summary_started
                    <= TRANSACTION_BREAKDOWN_BUDGET_SECONDS
                )
                transaction_breakdown = _derive_transaction_breakdown(
                    data,
                    source,
                    count,
                    data.get("runMeta", {}).get("finishedAt"),
                    summary_started + TRANSACTION_BREAKDOWN_BUDGET_SECONDS,
                )
                require(
                    transaction_breakdown["sale"]
                    + transaction_breakdown["lease"]
                    - transaction_breakdown["both"]
                    == count
                )
                breakdown_basis = "bounded_artifact_read"
                breakdown_reason = None
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                IndexError,
                AttributeError,
                safe.StopRefused,
            ):
                breakdown_reason = "bounded_artifact_unavailable"
        for descriptor, name, limit, raw in snapshots:
            require(safe._read_at(descriptor, name, limit)[0] == raw)
        result = {
            "schema_version": 1,
            "verified": True,
            "source": source,
            "collector_git_sha": expected_sha,
            "windows_admission_unix_ms": windows_admission_ms,
            "series_id": selected.name,
            "invocation_id": invocation,
            "run_id": run_id,
            "verified_listings": count,
            "transaction_scope": ["sale", "lease"],
            "measurement": "generation_exact_current_inventory",
            "quality_no_regression": True,
            "local_settlement": "confirmed",
            "transaction_breakdown_status": _breakdown_status(
                transaction_breakdown,
                breakdown_basis,
                breakdown_reason,
                transaction_started,
            ),
        }
        if transaction_breakdown is not None:
            result["transaction_breakdown"] = transaction_breakdown
        return result


def verified_pipeline_summary(
    series_directory, source_keys, expected_sha, windows_admission_ms
):
    """Return independently qualified source rows from one settled pipeline child.

    Counts are deliberately source-keyed. They are not additive because the same
    property may occur in more than one brokerage inventory.
    """
    summary_started = time.monotonic()
    selected_sources = list(source_keys)
    require(
        selected_sources
        and len(selected_sources) <= 51
        and len(set(selected_sources)) == len(selected_sources)
        and all(PIPELINE_SOURCE_KEY.fullmatch(source) for source in selected_sources)
    )
    require(
        isinstance(expected_sha, str) and re.fullmatch(r"[0-9a-f]{40}", expected_sha)
    )
    require(type(windows_admission_ms) is int and windows_admission_ms > 0)
    selected = Path(series_directory)
    require(selected.parent == COLLECTOR / "out/checkpoint-series")
    require(SERIES_ID.fullmatch(selected.name))
    snapshots = []
    with ExitStack() as stack:

        def directory(parent, name):
            descriptor = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            stack.callback(os.close, descriptor)
            safe._safe_stat(descriptor, directory=True)
            return descriptor

        def read(parent, name, limit=1048576):
            raw, _ = safe._read_at(parent, name, limit)
            value = json.loads(raw)
            require(isinstance(value, dict))
            snapshots.append((parent, name, limit, raw))
            return value

        root = directory(None, str(COLLECTOR))
        output = directory(root, "out")
        series_root = directory(output, "checkpoint-series")
        series_fd = directory(series_root, selected.name)
        parent = read(series_fd, "manifest.json")
        require(
            type(parent.get("schema_version")) is int
            and parent["schema_version"] == 1
            and parent.get("series_id") == selected.name
            and parent.get("status") in {"complete", "failed"}
            and parent.get("collector_git_sha") == expected_sha
            and parent.get("collector_git_dirty") is False
        )
        config = parent["config"]
        require(
            config.get("pipeline") is True
            and config.get("sources") == selected_sources
            and config.get("transactions") == ["sale", "lease"]
        )
        admissions = _pipeline_admissions(config, selected_sources)
        admission = parent["latest_invocation_admission"]
        require(admission.get("expected_sha") == expected_sha)
        require(admission.get("windows_admission_unix_ms") == windows_admission_ms)
        invocation = parent["latest_invocation_control"]["invocation_id"]
        require(isinstance(invocation, str) and HEX_ID.fullmatch(invocation))
        require(
            parent["latest_invocation_control"].get("receipt")
            == f".series-control/{invocation}.json"
        )
        private = directory(series_fd, ".series-control")
        safe._safe_stat(private, directory=True, private=True)
        control = read(private, invocation + ".json", 16384)
        owner = control["stop_owner"]
        require(
            type(owner.get("schema_version")) is int
            and owner["schema_version"] == 1
            and owner.get("uid") == os.geteuid()
            and owner.get("owner_role") == "checkpoint_series_one_shot_cli"
            and owner.get("invocation_id") == invocation
            and owner.get("series_directory_identity")
            == safe._identity(os.fstat(series_fd))
            and owner.get("control_directory_identity")
            == safe._identity(os.fstat(private))
        )
        require(
            control.get("status") == "complete"
            and control.get("local_settlement") == "confirmed"
        )
        started, finished = control["started_unix_ms"], control["finished_unix_ms"]
        require(type(started) is int and type(finished) is int)
        now_ms = int(time.time() * 1000)
        require(windows_admission_ms <= started <= finished <= now_ms + 60000)
        require(0 <= now_ms - finished <= 300000)
        parent_start, parent_finish = (
            timestamp(parent["started_at"]),
            timestamp(parent["finished_at"]),
        )
        require(parent_start <= started / 1000 <= parent_finish <= finished / 1000)
        parent_sources = parent["sources"]
        require(isinstance(parent_sources, dict) and set(parent_sources) == set(selected_sources))
        locators = set()
        child_ids = set()
        for source in selected_sources:
            entry = parent_sources[source]
            require(isinstance(entry, dict))
            locator = entry.get("checkpoint_run")
            require(isinstance(locator, str) and re.fullmatch(r"runs/[0-9TZ-]+-[0-9a-f]{12}", locator))
            locators.add(locator)
            attempts = entry.get("attempts")
            require(isinstance(attempts, list) and attempts)
            attempt = attempts[-1]
            require(attempt.get("checkpoint_run") == locator)
            child_id = attempt.get("series_control_child_id")
            require(isinstance(child_id, str) and HEX_ID.fullmatch(child_id))
            child_ids.add(child_id)
        require(len(locators) == 1 and len(child_ids) == 1)
        run_relative = locators.pop()
        run_id = run_relative[5:]
        require(RUN_ID.fullmatch(run_id))
        child_id = child_ids.pop()
        last_checkpoint = control.get("last_checkpoint", {})
        require(
            last_checkpoint.get("run_id") == run_id
            and last_checkpoint.get("child_invocation_id") == child_id
        )
        runs = directory(series_fd, "runs")
        child_fd = directory(runs, run_id)
        child = read(child_fd, "manifest.json")
        require(
            type(child.get("schema_version")) is int
            and child["schema_version"] == 2
            and child.get("run_id") == run_id
            and child.get("collector_git_sha") == expected_sha
            and child.get("collector_git_dirty") is False
            and child.get("status")
            in {
                "supported_scope_complete",
                "additive_scope_complete_current_inventory",
                "selected_transaction_scope_complete",
                "additive_scope_complete_coverage_hold",
                "failed",
            }
            and child.get("config", {}).get("pipeline") is True
            and child["config"].get("sources") == selected_sources
            and child["config"].get("transactions") == ["sale", "lease"]
            and child["config"].get("current_inventory_admissions") == admissions
            and child.get("preflight", {}).get("database_target")
            == parent.get("database_target")
            and bool(parent.get("database_target"))
        )
        require(
            parent_start
            <= timestamp(child["started_at"])
            <= timestamp(child["finished_at"])
            <= parent_finish
        )
        child_private = directory(child_fd, ".series-control")
        safe._safe_stat(child_private, directory=True, private=True)
        settlement = read(child_private, child_id + ".json", 16384)
        require(
            type(settlement.get("schema_version")) is int
            and settlement["schema_version"] == 1
            and settlement.get("invocation_id") == invocation
            and settlement.get("child_invocation_id") == child_id
            and settlement.get("run_id") == run_id
            and settlement.get("collector_git_sha") == expected_sha
            and type(settlement.get("pid")) is int
            and settlement["pid"] > 1
            and settlement["pid"] == last_checkpoint.get("pid")
            and all(
                settlement.get(key) is True
                for key in (
                    "guards_stopped",
                    "nested_cleanup_confirmed",
                    "terminal_evidence_saved",
                )
            )
        )
        pipeline = child.get("pipeline")
        require(isinstance(pipeline, dict))
        pipeline_status = pipeline.get("status")
        require(pipeline_status in {"complete", "partial", "failed"})
        source_results = pipeline.get("source_results")
        units = pipeline.get("units")
        require(isinstance(source_results, dict) and isinstance(units, list))
        require(set(source_results) <= set(selected_sources))
        checkpoints = child.get("sources")
        require(isinstance(checkpoints, dict) and set(checkpoints) == set(selected_sources))
        units_by_id = {}
        unit_memberships = {source: [] for source in selected_sources}
        for unit in units:
            require(isinstance(unit, dict))
            unit_id = unit.get("unit_id")
            require(
                isinstance(unit_id, str)
                and re.fullmatch(r"unit-[0-9]{4,6}", unit_id)
                and unit_id not in units_by_id
            )
            members = unit.get("sources")
            require(
                isinstance(members, list)
                and members
                and len(members) == len(set(members))
                and set(members) <= set(selected_sources)
            )
            if unit.get("state") == "verified":
                for member in members:
                    member_result = source_results.get(member)
                    require(
                        isinstance(member_result, dict)
                        and member_result.get("state") == "verified"
                        and member_result.get("unit_id") == unit_id
                    )
                    unit_memberships[member].append(unit_id)
            units_by_id[unit_id] = unit

        qualified = []
        failed = []
        pending = []
        validated_units = set()
        total_validation_bytes = 0
        for source in selected_sources:
            result = source_results.get(source)
            checkpoint = checkpoints[source]
            parent_entry = parent_sources[source]
            require(parent_entry.get("checkpoint_run") == run_relative)
            require(parent_entry.get("attempts", [])[-1].get("series_control_child_id") == child_id)
            state = result.get("state") if isinstance(result, dict) else None
            if state == "failed":
                require(parent_entry.get("state") in {"failed_source", "failed"})
                require(checkpoint.get("state") not in {"ingested", "complete"})
                failed.append(source)
                continue
            if result is None:
                require(
                    parent_entry.get("state")
                    in {"pending", "collecting", "candidate", "validated", "gated", "dry_run_passed"}
                )
                require(checkpoint.get("state") not in {"ingested", "complete"})
                pending.append(source)
                continue
            require(state == "verified")
            require(
                parent_entry.get("state") == "complete"
                and parent_entry.get("pipeline_result") == result
                and parent_entry.get("checkpoint_status") == child.get("status")
            )
            unit_id = result.get("unit_id")
            unit = units_by_id.get(unit_id)
            require(
                isinstance(unit, dict)
                and unit.get("state") == "verified"
                and source in unit.get("sources", [])
                and unit_memberships[source] == [unit_id]
                and result.get("readback_ok") is True
                and result.get("generation_id") == run_id
            )
            validation_path = result.get("validation_path")
            validation_sha = result.get("validation_sha256")
            require(
                isinstance(validation_path, str)
                and validation_path == unit.get("validation_path")
                and isinstance(validation_sha, str)
                and validation_sha == unit.get("validation_sha256")
                and re.fullmatch(r"[0-9a-f]{64}", validation_sha)
                and validation_path == f"pipeline/{unit_id}/validation.json"
            )
            if unit_id not in validated_units:
                pipeline_fd = directory(child_fd, "pipeline")
                unit_fd = directory(pipeline_fd, unit_id)
                validation_raw, _ = safe._read_at(
                    unit_fd, "validation.json", limit=MAX_VALIDATION_REPORT_BYTES
                )
                snapshots.append(
                    (unit_fd, "validation.json", MAX_VALIDATION_REPORT_BYTES, validation_raw)
                )
                total_validation_bytes += len(validation_raw)
                require(total_validation_bytes <= MAX_TOTAL_VALIDATION_REPORT_BYTES)
                require(hashlib.sha256(validation_raw).hexdigest() == validation_sha)
                validation = json.loads(validation_raw)
                from cre_validate import require_full_validation_report

                require(
                    validation.get("validation_profile") == "full"
                    and validation.get("audit_scope")
                    == "full_standalone_post_ingest_or_recovery"
                )
                require_full_validation_report(validation)
                require(validation.get("ok") is True)
                validated_units.add(unit_id)
            checkpoint = checkpoints[source]
            require(checkpoint.get("state") == "ingested")
            require(type(checkpoint.get("ingest", {}).get("rc")) is int and checkpoint["ingest"]["rc"] == 0)
            artifact, readback = checkpoint["artifact"], checkpoint["readback"]
            count = readback["expected_staged_unique"]
            artifact_sha = artifact.get("sha256")
            artifact_bytes = artifact.get("bytes")
            require(type(count) is int and 0 < count <= 500000)
            require(
                isinstance(artifact_sha, str)
                and re.fullmatch(r"[0-9a-f]{64}", artifact_sha)
                and type(artifact_bytes) is int
                and 0 < artifact_bytes <= MAX_ARTIFACT_BINDING_BYTES
                and result.get("artifact_sha256") == artifact_sha
            )
            require(
                readback.get("ok") is True
                and readback.get("latest_inventory_batch_active") == count
                and readback.get("generation_id") == run_id
                and artifact.get("path") == f"sources/{source}.json"
                and artifact.get("staged_unique") == count
                and artifact.get("inventory_only") == 0
                and artifact.get("rejected_by_ingest") == 0
            )
            qualified.append((source, count, checkpoint, unit))

        for descriptor, name, limit, raw in snapshots:
            require(safe._read_at(descriptor, name, limit)[0] == raw)

        result_rows = []
        total_artifact_bytes = 0
        for source, count, checkpoint, unit in qualified:
            row = {"source_key": source, "verified_listings": count}
            breakdown = None
            started_breakdown = time.monotonic()
            artifact = checkpoint["artifact"]
            breakdown_basis = "none"
            breakdown_reason = "staging_stats_absent"
            staged_stats = artifact.get("transaction_breakdown")
            if staged_stats is not None:
                try:
                    breakdown = _staged_transaction_breakdown(
                        artifact, source, count, run_id
                    )
                    breakdown_basis = "artifact_staging_statistics"
                    breakdown_reason = None
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    IndexError,
                    AttributeError,
                    safe.StopRefused,
                ):
                    breakdown_reason = "staging_stats_invalid"
            else:
                try:
                    artifact_bytes = artifact.get("bytes")
                    artifact_sha = artifact.get("sha256")
                    require(
                        type(artifact_bytes) is int
                        and 0 < artifact_bytes <= MAX_TRANSACTION_ARTIFACT_BYTES
                        and isinstance(artifact_sha, str)
                        and re.fullmatch(r"[0-9a-f]{64}", artifact_sha)
                    )
                    total_artifact_bytes += artifact_bytes
                    require(total_artifact_bytes <= MAX_TRANSACTION_ARTIFACT_BYTES)
                    require(
                        time.monotonic() - summary_started
                        <= TRANSACTION_BREAKDOWN_BUDGET_SECONDS
                    )
                    sources_fd = directory(child_fd, "sources")
                    raw, observed = safe._read_at(
                        sources_fd,
                        f"{source}.json",
                        limit=MAX_TRANSACTION_ARTIFACT_BYTES,
                    )
                    snapshots.append(
                        (sources_fd, f"{source}.json", MAX_TRANSACTION_ARTIFACT_BYTES, raw)
                    )
                    require(observed.st_size == artifact_bytes)
                    require(hashlib.sha256(raw).hexdigest() == artifact_sha)
                    data = json.loads(raw)
                    freshness = data.get("runMeta", {}).get("freshness")
                    require(
                        isinstance(freshness, dict)
                        and freshness.get("generationId") == run_id
                        and 0
                        <= time.time() - timestamp(data["runMeta"]["finishedAt"])
                        <= 86400
                    )
                    breakdown = _derive_transaction_breakdown(
                        data,
                        source,
                        count,
                        data.get("runMeta", {}).get("finishedAt"),
                        summary_started + TRANSACTION_BREAKDOWN_BUDGET_SECONDS,
                    )
                    require(
                        breakdown["sale"] + breakdown["lease"] - breakdown["both"]
                        == count
                    )
                    breakdown_basis = "bounded_artifact_read"
                    breakdown_reason = None
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    IndexError,
                    AttributeError,
                    safe.StopRefused,
                ):
                    breakdown_reason = "bounded_artifact_unavailable"
            row["transaction_breakdown_status"] = _breakdown_status(
                breakdown, breakdown_basis, breakdown_reason, started_breakdown
            )
            if breakdown is not None:
                row["transaction_breakdown"] = breakdown
            result_rows.append(row)
        for descriptor, name, limit, raw in snapshots:
            require(safe._read_at(descriptor, name, limit)[0] == raw)
        failed_sources = [s for s in selected_sources if s in set(failed)]
        pending_sources = [s for s in selected_sources if s in set(pending)]
        complete = (
            pipeline_status == "complete"
            and not failed_sources
            and not pending_sources
            and len(qualified) == len(selected_sources)
            and parent.get("status") == "complete"
        )
        return {
            "schema_version": 1,
            "verified": True,
            "collector_git_sha": expected_sha,
            "windows_admission_unix_ms": windows_admission_ms,
            "series_id": selected.name,
            "invocation_id": invocation,
            "run_id": run_id,
            "selected_source_keys": selected_sources,
            "per_source_summaries": result_rows,
            "failed_source_keys": failed_sources,
            "pending_source_keys": pending_sources,
            "complete": complete,
            "local_settlement": "confirmed",
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series-directory", required=True)
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--source")
    source_group.add_argument("--pipeline-source-keys")
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--windows-admission-unix-ms", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        if args.pipeline_source_keys is not None:
            result = verified_pipeline_summary(
                args.series_directory,
                args.pipeline_source_keys.split(","),
                args.expected_sha,
                args.windows_admission_unix_ms,
            )
        else:
            result = verified_summary(
                args.series_directory,
                args.source,
                args.expected_sha,
                args.windows_admission_unix_ms,
            )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        AttributeError,
        safe.StopRefused,
    ):
        result = {"schema_version": 1, "verified": False}
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
