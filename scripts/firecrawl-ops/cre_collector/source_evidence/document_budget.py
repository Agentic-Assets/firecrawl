"""Pure, serial reservations for a caller-persisted document acquisition run.

The caller must rederive the selection before initialization, persist every
transition before starting work, enforce phase deadlines, and reconcile unknown
outcomes without repeating attempts. Entity-byte reservations include the fetch
reader's one-byte oversize probe; HTTP headers and wire framing are not counted.
"""

import copy
import math

from source_evidence import document_acquisition_policy, events

SCHEMA = "cre-document-budget/v1"
OUTCOMES = {"acquired", "failed", "unknown"}


def _seconds(value):
    if (type(value) not in (int, float) or (type(value) is float and not math.isfinite(value))
            or value < 0):
        raise ValueError("invalid monotonic elapsed seconds")
    return value


def initial(selection):
    """Initialize from a previously rederived selection; no authority is inferred."""
    if not isinstance(selection, dict) or selection.get("schema_version") != "cre-document-acquisition-plan/v1":
        raise ValueError("invalid document selection")
    limits = selection.get("limits")
    if not isinstance(limits, dict) or set(limits) != set(document_acquisition_policy.LIMITS):
        raise ValueError("invalid document limits")
    for key, (low, high) in document_acquisition_policy.LIMITS.items():
        if type(limits[key]) is not int or not low <= limits[key] <= high:
            raise ValueError("invalid document limit")
    documents = selection.get("documents")
    if not isinstance(documents, list) or len(documents) > limits["max_documents"]:
        raise ValueError("invalid selected documents")
    indices = []
    for document in documents:
        if not isinstance(document, dict) or type(document.get("candidate_index")) is not int or document["candidate_index"] < 0:
            raise ValueError("invalid document index")
        indices.append(document["candidate_index"])
    if len(indices) != len(set(indices)):
        raise ValueError("duplicate selected index")
    return {"schema_version": SCHEMA, "selection_sha256": events.sha256(events.encoded(selection)),
            "limits": copy.deepcopy(limits), "indices": indices, "attempts": [], "last_elapsed": 0}


def _validate(state, selection):
    expected = initial(selection)
    if (not isinstance(state, dict) or set(state) != set(expected)
            or any(events.encoded(state[key]) != events.encoded(expected[key])
                   for key in ("schema_version", "selection_sha256", "limits", "indices"))
            or not isinstance(state["attempts"], list)):
        raise ValueError("document budget checkpoint mismatch")
    last = _seconds(state["last_elapsed"])
    used = 0
    previous = 0
    seen = set()
    for position, attempt in enumerate(state["attempts"]):
        if (not isinstance(attempt, dict) or set(attempt) != {"index", "max_bytes", "started", "phase_seconds", "outcome", "settled"}
                or type(attempt["index"]) is not int or attempt["index"] not in expected["indices"]
                or attempt["index"] in seen or type(attempt["max_bytes"]) is not int
                or not 1 <= attempt["max_bytes"] <= expected["limits"]["max_document_bytes"]):
            raise ValueError("invalid document attempt")
        start, phase = _seconds(attempt["started"]), _seconds(attempt["phase_seconds"])
        if start < previous or phase <= 0 or start + phase > expected["limits"]["run_timeout_seconds"]:
            raise ValueError("invalid document attempt timing")
        if attempt["outcome"] is None:
            if attempt["settled"] is not None or position != len(state["attempts"]) - 1:
                raise ValueError("invalid active document attempt")
            previous = start
        else:
            if not isinstance(attempt["outcome"], str) or attempt["outcome"] not in OUTCOMES:
                raise ValueError("invalid document outcome")
            previous = _seconds(attempt["settled"])
            if previous < start:
                raise ValueError("document settlement predates attempt")
        used += attempt["max_bytes"] + 1
        seen.add(attempt["index"])
    if (len(seen) > expected["limits"]["max_documents"] or used > expected["limits"]["max_total_bytes"]
            or last < previous):
        raise ValueError("invalid document budget totals")
    return used, seen


def reserve(state, selection, index, *, elapsed_seconds, phase_seconds):
    """Reserve before one fetch; phase includes all caller-supervised work and grace."""
    used, seen = _validate(state, selection)
    elapsed, phase = _seconds(elapsed_seconds), _seconds(phase_seconds)
    if phase <= 0 or elapsed < state["last_elapsed"]:
        raise ValueError("invalid reservation clock or duration")
    if type(index) is not int or index not in state["indices"]:
        raise ValueError("document index is not selected")
    if state["attempts"] and state["attempts"][-1]["outcome"] is None:
        raise ValueError("document attempt is still active")
    result = copy.deepcopy(state)
    result["last_elapsed"] = elapsed
    remaining = state["limits"]["max_total_bytes"] - used
    cap = min(state["limits"]["max_document_bytes"], remaining - 1)
    if (index in seen or len(seen) >= state["limits"]["max_documents"] or cap < 1
            or elapsed + phase > state["limits"]["run_timeout_seconds"]):
        return result, {"status": "budget_exhausted"}
    result["attempts"].append({"index": index, "max_bytes": cap, "started": elapsed,
                               "phase_seconds": phase, "outcome": None, "settled": None})
    return result, {"status": "reserved", "max_bytes": cap, "reserved_entity_bytes": cap + 1}


def settle(state, selection, index, *, elapsed_seconds, outcome):
    """Close an attempt without releasing its conservative byte reservation."""
    _validate(state, selection)
    elapsed = _seconds(elapsed_seconds)
    if (not isinstance(outcome, str) or outcome not in OUTCOMES or type(index) is not int or not state["attempts"]
            or state["attempts"][-1]["outcome"] is not None or state["attempts"][-1]["index"] != index
            or elapsed < state["last_elapsed"]):
        raise ValueError("invalid document settlement")
    result = copy.deepcopy(state)
    result["last_elapsed"] = elapsed
    result["attempts"][-1].update(outcome=outcome, settled=elapsed)
    return result
