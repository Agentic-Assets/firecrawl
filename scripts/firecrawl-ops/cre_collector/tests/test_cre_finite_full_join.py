"""Synthetic finite Observe → Authorize → owned maintenance → restoration."""
import json
from pathlib import Path

import pytest
from test_cre_continued_apply import assert_produced_transition_and_restoration
from test_cre_hold_prefix_full_join import (
    admitted,  # noqa: F401
    build_hold_successor,
    complete_case,  # noqa: F401
    continued_ready_case,  # noqa: F401
    current_inputs,  # noqa: F401
    lineage_case,  # noqa: F401
    qualified_ready_case,  # noqa: F401
    restoration_case,  # noqa: F401
    scheduler_principal_in_synthetic_xml,  # noqa: F401
)
from test_cre_maintenance_integrated import run_actual_apply_steps

import cre_finite_handoff as finite
from cre_code_handoff_evidence import HandoffError


@pytest.mark.parametrize("tamper_review", [False, True])
def test_finite_hold_successor_reaches_restoration(continued_ready_case, tmp_path, monkeypatch, tamper_review):  # noqa: F811
    case = build_hold_successor(continued_ready_case, tmp_path, monkeypatch)
    # The builder performs the real release validation before sealing this
    # synthetic loaded context, as load_release does in the native frontdoor.
    case.evidence._loaded_finite = case.evidence._loaded_maintenance
    prior_auth = json.loads(case.authorization.read_bytes())
    review = Path(prior_auth['review']['path'])
    case.authorization.unlink()
    observation_dir = tmp_path / 'finite-observation'
    case.observation = observation_dir / 'observation.json'

    def drive(args, git, modules):
        args.observation_output = str(observation_dir)
        args.observation = str(case.observation)
        args.review = str(review)
        args.reviewer = prior_auth['review']['reviewer']
        args.approval_ref = prior_auth['review']['approval_ref']
        finite.run_finite(args, case.release_raw, case.packet, case.evidence, git, modules)

    if tamper_review:
        body = Path(json.loads(review.read_bytes())["body"]["path"])
        body.write_bytes(b"changed after review")
        with pytest.raises(HandoffError):
            run_actual_apply_steps(case, tmp_path, monkeypatch, False, with_ipc=True, drive=drive)
        assert not case.output.exists()
        return
    run_actual_apply_steps(case, tmp_path, monkeypatch, False, with_ipc=True, drive=drive)
    assert_produced_transition_and_restoration(case, tmp_path)
