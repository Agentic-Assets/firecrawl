"""One Git recorder must see nested continuation history, not just the outer scope."""
from types import SimpleNamespace

import cre_runtime_maintenance_continuation as continuation


def test_continuation_threads_git_to_predecessor_and_current_stack(monkeypatch):
    old = {"sha": "a" * 40, "tree": "b" * 40}
    before = {"sha": "c" * 40, "tree": "d" * 40}
    names = ("failed_release", "failure_manifest", "recovery_completed", "recovery_prepared",
             "recovery_expected_state", "current_stack", "current_stack_review")
    values = {**{name: name for name in names}, "failed_operation": "/fixture"}
    packet = {"old": old, "runtime_before": before, "continuation": values}
    evidence = SimpleNamespace(inputs={name: {} for name in names}, obj=lambda _name: {})
    recorder = object()
    calls = []
    monkeypatch.setattr(continuation, "validate_continuation_code", lambda *_: None)
    monkeypatch.setattr(continuation, "validate_continuation_predecessor",
                        lambda *_args, git=None: calls.append(("predecessor", git)))
    import cre_current_stack
    monkeypatch.setattr(cre_current_stack, "validate_current_stack",
                        lambda *_args, git=None: calls.append(("stack", git)))
    continuation.validate_continuation(packet, evidence, recorder, "/fixture/repo")
    assert calls == [("predecessor", recorder), ("stack", recorder)]
