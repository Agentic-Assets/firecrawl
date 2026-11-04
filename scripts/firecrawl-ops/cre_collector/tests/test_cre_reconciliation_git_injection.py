"""Recorder is propagated through a reconciled historical restoration."""

import pytest
from test_cre_code_handoff import admitted, complete_case, lineage_case  # noqa: F401
from test_cre_code_handoff_restoration import (
    OLD,
    restoration_case,  # noqa: F401
    synthetic_reconciliation,
)

import cre_code_handoff_transition as transition


def test_reconciliation_passes_recorder_to_historical_restoration(restoration_case, monkeypatch):  # noqa: F811
    packet, receipt, files = synthetic_reconciliation(restoration_case)
    marker = object()
    seen = []

    def validate(_packet, _evidence, *, historical=False, git=None):
        seen.append((historical, git))
        raise RuntimeError("stop after recorder dispatch")

    monkeypatch.setattr(transition, "validate_restoration", validate)
    with pytest.raises(RuntimeError, match="recorder dispatch"):
        transition.validate_reconciliation(receipt, OLD, packet["code"], lambda path: files[str(path)], git=marker)
    assert seen == [(True, marker)]
