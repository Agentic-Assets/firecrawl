import copy

import pytest

from source_evidence import document_budget as budget


def selection(count=78, *, total=7800, per_document=99, run=1000):
    return {"schema_version": "cre-document-acquisition-plan/v1",
            "limits": {"max_documents": 100, "max_document_bytes": per_document,
                       "max_total_bytes": total, "document_timeout_seconds": 10,
                       "run_timeout_seconds": run, "max_redirects": 2},
            "documents": [{"candidate_index": i} for i in range(count)]}


def test_78_serial_attempts_retain_full_reservation_for_all_outcomes():
    chosen = selection()
    state = budget.initial(chosen)
    initial = copy.deepcopy(state)
    for index in range(78):
        old = copy.deepcopy(state)
        state, result = budget.reserve(state, chosen, index, elapsed_seconds=index * 2, phase_seconds=2)
        assert result == {"status": "reserved", "max_bytes": 99, "reserved_entity_bytes": 100}
        assert old["attempts"] == state["attempts"][:-1]
        state = budget.settle(state, chosen, index, elapsed_seconds=index * 2 + 1,
                              outcome=("acquired", "failed", "unknown")[index % 3])
    assert initial["attempts"] == []
    state, result = budget.reserve(state, chosen, 0, elapsed_seconds=156, phase_seconds=1)
    assert result["status"] == "budget_exhausted"
    assert len(state["attempts"]) == 78


def test_remaining_capacity_shrinks_cap_and_counts_probe():
    chosen = selection(3, total=106, per_document=99)
    state = budget.initial(chosen)
    state, _ = budget.reserve(state, chosen, 0, elapsed_seconds=0, phase_seconds=1)
    state = budget.settle(state, chosen, 0, elapsed_seconds=1, outcome="failed")
    state, reservation = budget.reserve(state, chosen, 1, elapsed_seconds=1, phase_seconds=1)
    assert reservation["max_bytes"] == 5
    assert reservation["reserved_entity_bytes"] == 6
    state = budget.settle(state, chosen, 1, elapsed_seconds=2, outcome="unknown")
    state, refusal = budget.reserve(state, chosen, 2, elapsed_seconds=2, phase_seconds=1)
    assert refusal["status"] == "budget_exhausted"


def test_active_attempt_cannot_be_repeated_and_late_settlement_stops_future_work():
    chosen = selection(2, run=4)
    state, _ = budget.reserve(budget.initial(chosen), chosen, 0, elapsed_seconds=0, phase_seconds=4)
    with pytest.raises(ValueError):
        budget.reserve(state, chosen, 1, elapsed_seconds=1, phase_seconds=1)
    state = budget.settle(state, chosen, 0, elapsed_seconds=5, outcome="unknown")
    _, refusal = budget.reserve(state, chosen, 1, elapsed_seconds=5, phase_seconds=1)
    assert refusal["status"] == "budget_exhausted"


@pytest.mark.parametrize("elapsed,phase", [(float("nan"), 1), (0, float("inf")), (-1, 1), (0, 0), (True, 1)])
def test_invalid_monotonic_times_are_refused(elapsed, phase):
    chosen = selection(1)
    with pytest.raises(ValueError):
        budget.reserve(budget.initial(chosen), chosen, 0, elapsed_seconds=elapsed, phase_seconds=phase)


def test_checkpoint_binding_and_monotonic_time_are_required():
    chosen = selection(2)
    state, _ = budget.reserve(budget.initial(chosen), chosen, 0, elapsed_seconds=2, phase_seconds=2)
    with pytest.raises(ValueError):
        budget.settle(state, chosen, 0, elapsed_seconds=1, outcome="failed")
    changed = copy.deepcopy(chosen)
    changed["limits"]["max_total_bytes"] += 1
    with pytest.raises(ValueError):
        budget.settle(state, changed, 0, elapsed_seconds=3, outcome="failed")
    changed_state = copy.deepcopy(state)
    changed_state["attempts"][0]["max_bytes"] = False
    with pytest.raises(ValueError):
        budget.settle(changed_state, chosen, 0, elapsed_seconds=3, outcome="failed")
