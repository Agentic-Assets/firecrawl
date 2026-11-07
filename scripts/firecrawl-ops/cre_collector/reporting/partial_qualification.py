"""Source-bound partial qualification and exact readback comparisons.

These checks do not admit writes or grant whole-source coverage.
"""

import json
import re
from collections.abc import Mapping
from typing import Any


def _exact_json_equal(left: Any, right: Any) -> bool:
    """Compare persisted JSON without Python's bool/number equality aliasing."""
    try:
        return json.dumps(
            left, allow_nan=False, separators=(",", ":"), sort_keys=True
        ) == json.dumps(right, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        return False


def matthews_partial_child(child: Mapping[str, Any]) -> bool:
    """Recognize only a bound Matthews existing-parent partial."""
    try:
        source = child["sources"]["matthews"]
        artifact, gate = source["artifact"], source["gate"]
        binding = gate["current_inventory_proof"]
        attempt = source["attempts"][-1]
        candidate = attempt["matthews_partial_candidate"]
        return (child["config"].get("pipeline") is True and artifact.get("matthews_partial") is True
                and isinstance(child.get("run_id"), str) and bool(child["run_id"])
                and isinstance(child.get("collector_git_sha"), str)
                and re.fullmatch(r"[a-f0-9]{40}", child["collector_git_sha"])
                and gate.get("verdict") == "ok_additive_verified_subset" and gate.get("mark_missing_safe") is False
                and gate.get("admission_scope") == "matthews_existing_parent_partial_v1"
                and binding.get("schema") == "matthews-existing-parent-partial-v1"
                and binding.get("scope") == "matthews_existing_parent_partial_v1"
                and binding.get("source_key") == "matthews" and binding.get("whole_source_coverage") is False
                and binding.get("artifact_sha256") == artifact.get("sha256") == candidate.get("gate_sha256")
                and binding.get("candidate_sha256") == candidate.get("sha256")
                and binding.get("source_artifact_sha256") == candidate.get("original_sha256") == attempt.get("rejected_artifact_sha256")
                and all(isinstance(binding.get(key), str) and re.fullmatch(r"[a-f0-9]{64}", binding[key])
                        for key in ("artifact_sha256", "candidate_sha256", "source_artifact_sha256", "database_target_sha256"))
                and binding.get("generation_id") == child["run_id"]
                and binding.get("producer_sha") == child["collector_git_sha"]
                and binding.get("database_target_sha256") == child["preflight"]["database_target"]["value"]
                and type(binding.get("candidate_count")) is int and binding["candidate_count"] > 0
                and binding["candidate_count"] == candidate.get("candidate_count") == artifact.get("staged_unique")
                and type(binding.get("source_held_count")) is int and binding["source_held_count"] >= 0
                and binding["source_held_count"] == candidate.get("held_count")
                and isinstance(source.get("subset_ingest_attempt_id"), str)
                and re.fullmatch(r"[a-f0-9]{32}", source["subset_ingest_attempt_id"]) is not None)
    except (AttributeError, KeyError, TypeError, ValueError, IndexError):
        return False


def franklin_partial_child(child: Mapping[str, Any]) -> bool:
    """Recognize only a target-bound Franklin partial with unresolved coverage."""
    try:
        source = child["sources"]["franklin-street"]
        artifact, gate = source["artifact"], source["gate"]
        binding = gate["current_inventory_proof"]
        if not isinstance(binding, Mapping) or not isinstance(binding.get("identity_uncertainty"), Mapping):
            return False
        return (isinstance(child.get("collector_git_sha"), str)
                and re.fullmatch(r"[0-9a-f]{40}", child["collector_git_sha"]) is not None
                and isinstance(child.get("run_id"), str) and bool(child["run_id"])
                and all(isinstance(binding.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", binding[key]) is not None
                        for key in ("artifact_sha256", "source_artifact_sha256", "database_target_sha256", "staged_sha256"))
                and artifact.get("franklin_partial") is True
                and gate.get("verdict") == "ok_additive_verified_subset"
                and gate.get("mark_missing_safe") is False
                and gate.get("admission_scope") == "franklin_site_partial_v1"
                and binding.get("schema") == "franklin-site-partial-v1"
                and binding.get("scope") == "franklin_site_partial_v1"
                and binding.get("source_key") == "franklin-street"
                and binding.get("whole_source_coverage") is False
                and binding.get("artifact_sha256") == artifact.get("sha256")
                and binding.get("source_artifact_sha256") == source["franklin_original"]["sha256"]
                and binding.get("generation_id") == child.get("run_id")
                and binding.get("collector_git_sha") == child.get("collector_git_sha")
                and binding.get("producer_sha") == child.get("collector_git_sha")
                and binding.get("database_target_sha256") == child["preflight"]["database_target"]["value"]
                and type(binding.get("held_count")) is int and binding["held_count"] >= 0
                and isinstance(binding.get("held"), list) and len(binding["held"]) == binding["held_count"]
                and type(binding.get("emitted_count")) is int and binding["emitted_count"] >= 0
                and type(binding.get("buildout_pending")) is int and binding["buildout_pending"] >= 0
                and type(binding.get("held_observations")) is int and binding["held_observations"] >= 0
                and binding.get("identity_uncertainty", {}).get("siteToBuildout") == "unresolved"
                and type(artifact.get("staged_unique")) is int
                and artifact["staged_unique"] == binding["emitted_count"] + binding["held_count"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def colliers_partial_child(child: Mapping[str, Any]) -> bool:
    """Recognize a source-bound partial window with settled cursor custody."""
    try:
        checkpoint = child["sources"]["colliers"]
        artifact, gate = checkpoint["artifact"], checkpoint["gate"]
        binding, settlement = gate["current_inventory_proof"], checkpoint["partial_cursor_settlement"]
        held, history = binding["held"], binding["history_holds"]
        attempt = checkpoint["subset_ingest_attempt_id"]
        window_index = (checkpoint.get("colliers_partial_worker") or {}).get("window_index", 1)
        return (type(window_index) is int and window_index >= 1
                and child["config"].get("pipeline") is True
                and isinstance(child.get("run_id"), str) and bool(child["run_id"])
                and isinstance(child.get("started_at"), str) and bool(child["started_at"])
                and isinstance(child.get("collector_git_sha"), str)
                and re.fullmatch(r"[a-f0-9]{40}", child["collector_git_sha"])
                and artifact.get("colliers_partial") is True
                and gate.get("verdict") == "ok_additive_verified_subset"
                and gate.get("mark_missing_safe") is False
                and gate.get("admission_scope") == "colliers_partial_window_v1"
                and binding.get("schema") == "colliers-partial-window-v1"
                and binding.get("scope") == "colliers_partial_window_v1"
                and binding.get("source_key") == "colliers"
                and binding.get("original_source_outcome") == "source_error"
                and binding.get("whole_source_coverage") is False
                and binding.get("artifact_sha256") == artifact.get("sha256")
                and binding.get("generation_id") == child.get("run_id")
                and binding.get("generation_started_at") == child.get("started_at")
                and binding.get("producer_sha") == child.get("collector_git_sha")
                and binding.get("database_target_sha256") == child["preflight"]["database_target"]["value"]
                and all(isinstance(binding.get(key), str) and re.fullmatch(r"[a-f0-9]{64}", binding[key])
                        for key in ("artifact_sha256", "database_target_sha256", "staged_sha256",
                                    "evidence_sha256", "source_sha256", "source_main_sha256", "preimage_sha256"))
                and type(binding.get("attempt")) is int and binding["attempt"] > 0
                and artifact.get("path") == (f"colliers-partial/window-{binding['attempt']}"
                    + ("" if window_index == 1 else f"-{window_index}") + "-artifact.json")
                and isinstance(attempt, str) and re.fullmatch(r"[a-f0-9]{32}", attempt)
                and isinstance(held, list) and all(isinstance(value, str) and value for value in held)
                and len(held) == len(set(held)) and isinstance(history, dict)
                and all(isinstance(key, str) and key and isinstance(value, str) and value for key, value in history.items())
                and not set(held) & set(history)
                and type(binding.get("held_count")) is int and binding["held_count"] == len(held) + len(history)
                and type(binding.get("emitted_count")) is int and binding["emitted_count"] >= 0
                and type(artifact.get("staged_unique")) is int and artifact["staged_unique"] > 0
                and artifact["staged_unique"] == binding["emitted_count"] + binding["held_count"]
                and type(binding.get("cursor")) is int and binding["cursor"] > 0
                and type(binding.get("next_cursor")) is int and binding["next_cursor"] > 0
                and settlement.get("state") == "settled" and settlement.get("attempt_id") == attempt
                and settlement.get("artifact_sha256") == artifact.get("sha256")
                and settlement.get("cursor") == binding["cursor"] and settlement.get("next_cursor") == binding["next_cursor"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def lyon_partial_child(child: Mapping[str, Any]) -> bool:
    """Recognize only the source-bound Lyon search partial and its DB partition."""
    try:
        checkpoint = child["sources"]["lyon-stahl"]
        artifact, gate = checkpoint["artifact"], checkpoint["gate"]
        binding, worker, probe = gate["current_inventory_proof"], checkpoint["lyon_partial_worker"], checkpoint["lyon_identity_probe"]
        held, attempt = binding["held"], checkpoint["subset_ingest_attempt_id"]
        return (child["config"].get("pipeline") is True
                and isinstance(child.get("run_id"), str) and bool(child["run_id"])
                and isinstance(child.get("started_at"), str) and bool(child["started_at"])
                and isinstance(child.get("collector_git_sha"), str)
                and re.fullmatch(r"[a-f0-9]{40}", child["collector_git_sha"])
                and artifact.get("lyon_partial") is True and gate.get("verdict") == "ok_additive_verified_subset"
                and gate.get("mark_missing_safe") is False and gate.get("admission_scope") == "lyon_partial_search_v1"
                and binding.get("schema") == "lyon-search-partial-v1" and binding.get("scope") == "lyon_partial_search_v1"
                and binding.get("source_key") == "lyon-stahl" and binding.get("original_source_outcome") == "source_error"
                and binding.get("whole_source_coverage") is False
                and binding.get("artifact_sha256") == artifact.get("sha256") == worker.get("sha256") == probe.get("artifact_sha256")
                and binding.get("generation_id") == child.get("run_id") == probe.get("generation_id")
                and binding.get("generation_started_at") == child.get("started_at")
                and binding.get("producer_sha") == child.get("collector_git_sha") == probe.get("producer_sha")
                and binding.get("database_target_sha256") == child["preflight"]["database_target"]["value"] == probe.get("database_target_sha256")
                and binding.get("staged_sha256") == probe.get("staged_sha256")
                and binding.get("preimage_sha256") == probe.get("preimage_sha256")
                and all(isinstance(binding.get(key), str) and re.fullmatch(r"[a-f0-9]{64}", binding[key])
                        for key in ("artifact_sha256", "database_target_sha256", "staged_sha256", "source_sha256", "source_main_sha256", "preimage_sha256"))
                and type(binding.get("attempt")) is int and binding["attempt"] > 0
                and artifact.get("path") == f"lyon-stahl-partial/candidate-{binding['attempt']}.json"
                and worker.get("state") == "settled" and worker.get("attempt") == binding["attempt"]
                and isinstance(attempt, str) and re.fullmatch(r"[a-f0-9]{32}", attempt)
                and isinstance(held, list) and all(isinstance(value, str) and value for value in held)
                and held == sorted(set(held)) and _exact_json_equal(held, probe.get("held"))
                and type(binding.get("held_count")) is int and binding["held_count"] >= len(held)
                and type(binding.get("emitted_count")) is int and binding["emitted_count"] >= 0
                and type(artifact.get("staged_unique")) is int and artifact["staged_unique"] > 0
                and artifact["staged_unique"] == binding["emitted_count"] + len(held))
    except (KeyError, TypeError, ValueError, AttributeError):
        return False



def partial_member_report_matches(report: dict, checkpoint: dict, source: str, run_id: str) -> bool | None:
    """Return None when this source-specific partial proof does not apply."""
    artifact = checkpoint["artifact"]
    readback = checkpoint["readback"]
    if source == "matthews" and artifact.get("matthews_partial") is True:
        binding = (checkpoint.get("gate") or {}).get("current_inventory_proof") or {}
        observed = report.get("matthews_partial_readback")
        return (isinstance(observed, dict) and _exact_json_equal(observed, readback.get("observation"))
                and readback.get("ok") is True and readback.get("kind") == "matthews_existing_parent_partial"
                and readback.get("whole_source_coverage") is False and observed.get("whole_source_coverage") is False
                and all(type(observed.get(key)) is int and observed[key] == 1 for key in
                        ("occupied_jobs", "matching_jobs", "verified_jobs"))
                and observed.get("generation_id") == binding.get("generation_id") == run_id
                and observed.get("artifact_sha256") == artifact.get("sha256") == binding.get("artifact_sha256")
                and observed.get("database_target_sha256") == binding.get("database_target_sha256")
                and observed.get("attempt_id") == checkpoint.get("subset_ingest_attempt_id")
                and type(observed.get("candidate_count")) is int and observed["candidate_count"] == binding.get("candidate_count")
                and type(observed.get("source_held_count")) is int and observed["source_held_count"] == binding.get("source_held_count"))
    if source == "franklin-street" and artifact.get("franklin_partial") is True:
        binding = (checkpoint.get("gate") or {}).get("current_inventory_proof") or {}
        observed = report.get("franklin_partial_readback")
        return (isinstance(observed, dict) and _exact_json_equal(observed, readback.get("observation"))
                and readback.get("ok") is True and readback.get("kind") == "franklin_site_partial"
                and readback.get("whole_source_coverage") is False
                and observed.get("ok") is True and observed.get("invalid") == []
                and type(observed.get("matching_jobs")) is int and observed["matching_jobs"] == 1
                and observed.get("generation_id") == binding.get("generation_id") == run_id
                and observed.get("artifact_sha256") == artifact.get("sha256") == binding.get("artifact_sha256")
                and observed.get("database_target_sha256") == binding.get("database_target_sha256")
                and isinstance(checkpoint.get("subset_ingest_attempt_id"), str)
                and re.fullmatch(r"[0-9a-f]{32}", checkpoint["subset_ingest_attempt_id"]) is not None
                and observed.get("attempt_id") == checkpoint["subset_ingest_attempt_id"]
                and observed.get("staged_sha256") == binding.get("staged_sha256")
                and _exact_json_equal(observed.get("held"), binding.get("held"))
                and type(observed.get("expected")) is int and observed["expected"] == binding.get("emitted_count"))
    if source == "lyon-stahl" and artifact.get("lyon_partial") is True:
        binding = (checkpoint.get("gate") or {}).get("current_inventory_proof") or {}
        observed = report.get("lyon_partial_readback")
        return (isinstance(observed, dict) and _exact_json_equal(observed, readback.get("observation"))
                and readback.get("ok") is True and readback.get("kind") == "lyon_partial_search"
                and readback.get("whole_source_coverage") is False
                and observed.get("ok") is True and observed.get("outcome") == "exact_commit"
                and observed.get("generation_id") == binding.get("generation_id") == run_id
                and observed.get("artifact_sha256") == artifact.get("sha256") == binding.get("artifact_sha256")
                and observed.get("database_target_sha256") == binding.get("database_target_sha256")
                and observed.get("attempt_id") == checkpoint.get("subset_ingest_attempt_id")
                and observed.get("staged_sha256") == binding.get("staged_sha256")
                and _exact_json_equal(observed.get("held"), binding.get("held"))
                and type(observed.get("expected")) is int and observed["expected"] == binding.get("emitted_count")
                and observed.get("child_collections_verified") is True)
    if source == "colliers" and artifact.get("colliers_partial") is True:
        binding = (checkpoint.get("gate") or {}).get("current_inventory_proof") or {}
        observed = report.get("colliers_partial_readback")
        return (isinstance(observed, dict) and _exact_json_equal(observed, readback.get("observation"))
                and readback.get("ok") is True and readback.get("kind") == "colliers_partial_window"
                and readback.get("whole_source_coverage") is False
                and observed.get("ok") is True and observed.get("outcome") == "exact_commit"
                and observed.get("generation_id") == binding.get("generation_id") == run_id
                and observed.get("artifact_sha256") == artifact.get("sha256") == binding.get("artifact_sha256")
                and observed.get("database_target_sha256") == binding.get("database_target_sha256")
                and observed.get("attempt_id") == checkpoint.get("subset_ingest_attempt_id")
                and observed.get("staged_sha256") == binding.get("staged_sha256")
                and _exact_json_equal(observed.get("held"), binding.get("held"))
                and _exact_json_equal(observed.get("history_holds"), binding.get("history_holds"))
                and type(observed.get("expected")) is int and observed["expected"] == binding.get("emitted_count")
                and observed.get("child_collections_verified") is False)
    return None
