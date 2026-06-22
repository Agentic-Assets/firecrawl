"""
conftest.py: put the parent cre_collector dir on sys.path so
`from cre_ingest import ...` resolves correctly under pytest, regardless of
where pytest is invoked from.
"""
import sys
import os

# Insert cre_collector/ at the front of sys.path so cre_ingest is importable
# without any package install step.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)
