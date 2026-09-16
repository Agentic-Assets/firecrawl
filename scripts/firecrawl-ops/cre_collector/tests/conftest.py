"""
conftest.py: put the parent cre_collector dir on sys.path so
`from cre_ingest import ...` resolves correctly under pytest, regardless of
where pytest is invoked from.
"""

import os
import sys

import pytest

# Insert cre_collector/ at the front of sys.path so cre_ingest is importable
# without any package install step.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)


@pytest.fixture(autouse=True)
def _c10_structural_fixture(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep C10 protocol fixtures offline while production rechecks authority."""
    if request.module.__name__.startswith("test_capacity_c10"):
        from capacity_c10 import contracts

        monkeypatch.setattr(
            contracts, "_require_repository_plan_authority", lambda _plan: None
        )
