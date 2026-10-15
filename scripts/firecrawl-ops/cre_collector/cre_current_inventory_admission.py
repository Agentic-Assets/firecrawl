"""Explicit source-bound additive admission; never a baseline/lifecycle writer."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from cre_gate import count_artifacts
from cre_ingest import strict_full_source_scope
from cre_source_policy import load_source_policy, resolve_policy_path

SOURCE = "interra-realty"
ENDPOINT = "https://interrarealty.com/wp-json/filtered-loop/v1/posts?post_type=torque_listing&posts_per_page=1000&paged=1"
SCHEMA = "interra-current-inventory-v1"
VERDICT = "ok_additive_current_inventory"
SCOPE = "additive_current_inventory"


def require(condition: object) -> None:
    if not condition:
        raise ValueError(
            "Current-inventory admission evidence is incomplete or drifted"
        )


def configuration(source: str) -> dict:
    from cre_buildout_current_inventory import definitions, registry_path

    require(source == SOURCE or source in definitions())
    load_source_policy()
    result = {
        "source_key": source,
        "source_policy_sha256": hashlib.sha256(
            resolve_policy_path().read_bytes()
        ).hexdigest(),
    }
    if source != SOURCE:
        result["buildout_registry_sha256"] = hashlib.sha256(
            registry_path().read_bytes()
        ).hexdigest()
    return result


def validate_option(
    source: str | None, sources, transactions, *, additive_hold=False, allow_dirty=False
) -> None:
    if source is None:
        return
    require(list(sources) == [source] and tuple(transactions) == ("sale", "lease"))
    configuration(source)
    require(not additive_hold and not allow_dirty)


def validate_options(
    admitted_sources,
    sources,
    transactions,
    *,
    pipeline=False,
    additive_hold=False,
    allow_dirty=False,
) -> None:
    """Admit only explicit exact keys inside the selected source set."""
    admitted = list(admitted_sources or ())
    selected = list(sources)
    require(len(selected) == len(set(selected)))
    require(len(admitted) == len(set(admitted)))
    if not pipeline:
        require(len(admitted) <= 1)
        validate_option(
            admitted[0] if admitted else None,
            selected,
            transactions,
            additive_hold=additive_hold,
            allow_dirty=allow_dirty,
        )
        return
    if admitted:
        require(tuple(transactions) == ("sale", "lease"))
        require(not additive_hold and not allow_dirty)
    for source in admitted:
        require(source in selected)
        configuration(source)


def configuration_map(admitted_sources, sources) -> dict:
    validate_options(admitted_sources, sources, ("sale", "lease"), pipeline=True)
    return {source: configuration(source) for source in admitted_sources}


def admission_configuration(config: dict, source: str) -> dict | None:
    """Read the original flat owner's exact source entry, never inherited scope."""
    legacy = config.get("current_inventory_admission")
    entries = config.get("current_inventory_admissions")
    require(not (legacy is not None and entries is not None))
    if entries is not None:
        require(config.get("pipeline") is True and isinstance(entries, dict))
        validate_options(
            entries,
            config["sources"],
            config["transactions"],
            pipeline=True,
            additive_hold=config.get("admit_baseline_hold_additively"),
        )
        for key, entry in entries.items():
            require(entry == configuration(key))
        return entries.get(source)
    if legacy is None:
        return None
    require(isinstance(legacy, dict))
    key = legacy.get("source_key")
    validate_option(
        key,
        config["sources"],
        config["transactions"],
        additive_hold=config.get("admit_baseline_hold_additively"),
    )
    require(legacy == configuration(key))
    return legacy if key == source else None


def validate_report_scope(
    report: dict, manifest: dict, source: str, artifact_path: Path
) -> None:
    """The source raw verdict must belong to this exact sealed report."""
    if manifest["config"].get("pipeline") is True:
        members = set(report["per_source"])
        require(source in members and members <= set(manifest["config"]["sources"]))
        paths = report["generated_for_artifact"]
        paths = [paths] if isinstance(paths, str) else paths
        require(isinstance(paths, list) and len(paths) == len(set(paths)))
        require(str(artifact_path) in paths and len(paths) == len(members))
        # The coordinator binds each exact attempt path in the original owner.
        own = Path(manifest["sources"][source]["artifact"]["path"])
        require(not own.is_absolute() and ".." not in own.parts)
        root = artifact_path
        for _part in own.parts:
            root = root.parent
        require(root / own == artifact_path)
        expected = set()
        for key in members:
            relative = Path(manifest["sources"][key]["artifact"]["path"])
            require(not relative.is_absolute() and ".." not in relative.parts)
            expected.add(str(root / relative))
        require(set(paths) == expected)
    else:
        require(set(report["per_source"]) == {source})
        require(
            report["generated_for_artifact"]
            in (str(artifact_path), [str(artifact_path)])
        )


def _time(value: object) -> datetime:
    require(isinstance(value, str))
    parsed = datetime.fromisoformat(value)
    require(parsed.tzinfo is not None)
    return parsed.astimezone(UTC)


def _ids(values: object) -> list[str]:
    require(
        isinstance(values, list)
        and all(
            isinstance(v, str)
            and re.fullmatch(r"[1-9][0-9]{0,15}", v)
            and int(v) <= 9007199254740991
            for v in values
        )
    )
    require(values == sorted(set(values)))
    return values


def _admit_interra(
    artifact_path: Path,
    manifest: dict,
    report: dict,
    *,
    current_code_sha: str,
    current_git_dirty: bool,
    phase: str,
) -> dict:
    """Validate the exact current artifact plus raw live first_seen at both gates.

    The baseline reason is consumed only from the existing target-bound live
    gate command. Missing medians alone do not prove baseline absence. The feed
    receipt is source-owned provenance, not an independent provider count or
    cryptographic attestation. Stored original bindings must survive aggregate
    admission; no historical-baseline certificate is granted.
    """
    config = manifest["config"]
    admission = admission_configuration(config, SOURCE)
    require(admission == configuration(SOURCE))
    require(
        config.get("additive") is True
        and config.get("mark_missing") is False
        and config.get("status_activation") is False
    )
    require(config.get("max_items") == 0)
    require(manifest.get("collector_git_dirty") is False and current_git_dirty is False)
    require(
        re.fullmatch(r"[0-9a-f]{40}", current_code_sha)
        and manifest["collector_git_sha"] == current_code_sha
    )
    target = manifest["preflight"]["database_target"]
    require(
        isinstance(target, dict)
        and target.get("algorithm") == "sha256"
        and isinstance(target.get("value"), str)
        and re.fullmatch(r"[0-9a-f]{64}", target["value"])
    )
    require(phase in {"source", "aggregate"})
    info = manifest["sources"][SOURCE]["artifact"]
    require(not artifact_path.is_symlink() and artifact_path.is_file())
    raw = artifact_path.read_bytes()
    actual_hash = hashlib.sha256(raw).hexdigest()
    require(actual_hash == info["sha256"] and len(raw) == info["bytes"])
    artifact = json.loads(raw)
    scope = strict_full_source_scope(artifact, SOURCE)
    meta = artifact["runMeta"]
    freshness = meta.get("freshness") or {}
    start = _time(manifest["started_at"])
    require(
        freshness.get("generationId") == manifest["run_id"]
        and freshness.get("requireFreshDetails") is True
    )
    require(_time(freshness.get("generationStartedAt")) == start)
    entries = artifact["sources"]
    require(len(entries) == 2)
    sale, lease = (
        {row["transaction"]: row for row in entries}[t] for t in ("sale", "lease")
    )
    for row, transaction in ((sale, "sale"), (lease, "lease")):
        require(
            row.get("sourceKey") == SOURCE
            and row.get("supported") is True
            and not row.get("error")
            and row.get("truncated") is not True
        )
        receipt = row.get("currentInventoryProof")
        require(
            isinstance(receipt, dict)
            and receipt.get("schema") == SCHEMA
            and receipt.get("sourceKey") == SOURCE
        )
        require(
            receipt.get("transaction") == transaction
            and receipt.get("generationId") == manifest["run_id"]
        )
    proof = sale["currentInventoryProof"]
    require(
        proof.get("kind") == "terminal_feed"
        and proof.get("endpoint") == ENDPOINT
        and proof.get("hasNextPage") is False
    )
    require(
        isinstance(proof.get("responseTextSha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", proof["responseTextSha256"])
    )
    observed = _time(proof.get("observedAt"))
    now = datetime.now(UTC)
    require(
        start
        <= _time(meta["startedAt"])
        <= observed
        <= _time(meta["finishedAt"])
        <= now + timedelta(minutes=5)
    )
    require(now - timedelta(hours=24) <= observed <= now + timedelta(minutes=5))
    partitions = proof.get("nativeIdsByLifecycle")
    require(
        isinstance(partitions, dict)
        and set(partitions) == {"available", "under_contract", "closed"}
    )
    parts = {key: _ids(values) for key, values in partitions.items()}
    all_ids = [value for values in parts.values() for value in values]
    require(len(all_ids) == len(set(all_ids)))
    emitted = sorted(parts["available"] + parts["under_contract"])
    require(bool(emitted))
    require(
        proof.get("emittedNativeIdsSha256")
        == hashlib.sha256(
            json.dumps(emitted, separators=(",", ":")).encode()
        ).hexdigest()
    )
    empty = lease["currentInventoryProof"]
    require(
        empty.get("kind") == "known_empty_transaction"
        and empty.get("reason") == "sale_only_source"
        and type(empty.get("count")) is int
        and empty["count"] == 0
    )
    require(
        not any(
            key in empty for key in ("endpoint", "observedAt", "responseTextSha256")
        )
    )
    listings = scope["listings"]
    require(
        sorted(row.get("id") for row in listings) == emitted
        and len(listings) == len(emitted)
    )
    for row in listings:
        require(
            row.get("transactionMode") == "sale"
            and row.get("transactionType") == "Sale"
        )
        expected_badge = (
            "Available" if row["id"] in parts["available"] else "Under Contract"
        )
        require(row.get("statusBadge") == expected_badge)
        parsed = urlsplit(row.get("url", ""))
        require(
            parsed.scheme == "https"
            and parsed.netloc == "interrarealty.com"
            and parsed.path.startswith("/listing/")
        )
        require(
            row.get("canonicalUrl") == row.get("url")
            and row.get("preserveChildCollections") is True
        )
        require(
            _time(row.get("inventoryObservedAt")) == observed
            and _time(row.get("detailObservedAt")) == observed
        )
        provenance = row.get("freshnessProvenance") or {}
        require(
            provenance.get("generationId") == manifest["run_id"]
            and _time(provenance.get("observedAt")) == observed
        )
        require(
            provenance.get("method") == "interra_filtered_loop_inventory"
            and provenance.get("cacheDisposition") == "live"
            and provenance.get("detailScope") == "authoritative_inventory_feed"
        )
    current, errors, sources, _finished, conversion_errors = count_artifacts(
        [str(artifact_path)], True
    )
    require(sources == {SOURCE} and not any(errors.values()) and conversion_errors == 0)
    require(current.get(SOURCE) == len(emitted))
    validate_report_scope(report, manifest, SOURCE, artifact_path)
    summary = report["summary"]
    require(
        summary.get("mode") == "live"
        and type(summary.get("torow_errors")) is int
        and summary["torow_errors"] == 0
    )
    require(
        summary.get("baseline_updated") is False
        and summary.get("baseline_update_sources") == []
    )
    gate = report["per_source"][SOURCE]
    require(
        gate.get("verdict") == "first_seen"
        and gate.get("reason") == "no baseline row; cannot gate (first sight)"
    )
    require(
        gate.get("baseline_median") is None and gate.get("mark_missing_safe") is False
    )
    require(
        type(gate.get("current_active")) is int
        and gate["current_active"] == len(emitted)
    )
    binding = {
        "schema": SCHEMA,
        "scope": SCOPE,
        "artifact_sha256": actual_hash,
        "collector_git_sha": current_code_sha,
        "database_target_sha256": target["value"],
        "source_policy_sha256": admission["source_policy_sha256"],
        "generation_id": manifest["run_id"],
        "generation_started_at": manifest["started_at"],
        "observed_at": proof["observedAt"],
        "response_text_sha256": proof["responseTextSha256"],
        "emitted_native_ids_sha256": proof["emittedNativeIdsSha256"],
        "emitted_count": len(emitted),
    }
    if phase == "aggregate":
        require(
            manifest["sources"][SOURCE].get("gate", {}).get("current_inventory_proof")
            == binding
        )
    gate.update(
        raw_verdict=gate["verdict"],
        raw_reason=gate["reason"],
        verdict=VERDICT,
        reason="complete current inventory admitted additively; historical baseline remains absent",
        mark_missing_safe=False,
        admission_scope=SCOPE,
        current_inventory_proof=binding,
    )
    report["scope"] = {
        "kind": SCOPE,
        "transactions": ["sale", "lease"],
        "whole_source_coverage": False,
    }
    summary.update(mark_missing_safe_sources=[], mark_missing_safe_brokerages=[])
    manifest["scope"].update(
        kind="collector_registry_additive_current_inventory",
        whole_source_coverage=False,
    )
    return binding


def admit(
    artifact_path: Path, manifest: dict, report: dict, *, source=None, **kwargs
) -> dict:
    """Dispatch only an explicit one-source configuration, preserving defaults."""
    if source is None:
        source = (
            manifest["config"].get("current_inventory_admission", {}).get("source_key")
        )
    require(admission_configuration(manifest["config"], source) is not None)
    if source == SOURCE:
        return _admit_interra(artifact_path, manifest, report, **kwargs)
    from cre_buildout_current_inventory import admit as admit_buildout

    return admit_buildout(artifact_path, manifest, report, source=source, **kwargs)
