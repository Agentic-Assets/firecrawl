"""Architecture contracts for the source-bound reporting seam."""
import ast
import copy
import json
from pathlib import Path

import pytest

import cre_checkpoint_series as series
from reporting import partial_qualification as partial
from tests.test_colliers_partial_series import mixed_child as colliers_child
from tests.test_lyon_partial_series import RUN_ID, add_franklin
from tests.test_lyon_partial_series import mixed_child as lyon_child

ROOT = Path(__file__).resolve().parents[1]


def root_layout_errors(actual, entrypoints, legacy):
    """New root code needs an explicitly reviewed entrypoint classification."""
    return actual - entrypoints - legacy


def test_reusable_reporting_module_has_one_way_dependencies():
    source = (ROOT / "reporting" / "partial_qualification.py").read_text()
    tree = ast.parse(source)
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "__import__", "eval", "exec"}
    assert set(imports) <= {"json", "re", "collections.abc", "typing"}


def test_reporting_consumers_use_shared_source_checks_not_series_internals():
    expected = {"franklin_partial_child", "lyon_partial_child", "colliers_partial_child", "matthews_partial_child"}
    for name in ("reporting/source_health.py", "cre_notification_summary.py"):
        tree = ast.parse((ROOT / name).read_text())
        shared = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "reporting.partial_qualification":
                shared.update(alias.name for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.module == "cre_checkpoint_series":
                assert not expected.intersection(alias.name for alias in node.names)
        assert expected | {"partial_member_report_matches"} <= shared
    assert series.matthews_partial_child is partial.matthews_partial_child
    assert series.franklin_partial_child is partial.franklin_partial_child
    assert series.lyon_partial_child is partial.lyon_partial_child
    assert series.colliers_partial_child is partial.colliers_partial_child


def test_root_layout_blocks_new_unclassified_modules_and_allows_migration():
    policy = json.loads((ROOT / "tests" / "fixtures" / "collector-root-layout.json").read_text())
    entrypoints = set(policy["entrypoints"])
    legacy = set(policy["legacy_modules"])
    assert len(entrypoints) == len(policy["entrypoints"])
    assert len(legacy) == len(policy["legacy_modules"])
    assert not entrypoints & legacy
    patterns = ("*.py", "*.js", "*.mjs", "*.cjs", "*.ts", "*.tsx", "*.mts", "*.cts")
    actual = {path.name for pattern in patterns for path in ROOT.glob(pattern)}
    assert not root_layout_errors(actual, entrypoints, legacy)
    assert not legacy - actual, "Remove migrated modules from the legacy baseline"
    assert not entrypoints - actual, "Remove relocated entrypoints from the root policy"
    for name in entrypoints:
        if name.endswith(".py"):
            tree = ast.parse((ROOT / name).read_text())
            assert any(isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                       and ast.unparse(node.test) in {"__name__ == '__main__'", '__name__ == "__main__"'}
                       for node in tree.body), f"{name} is not a direct entrypoint"
    unclassified = {"unclassified_helper.py", "unclassified_helper.mjs"}
    assert root_layout_errors(actual | unclassified, entrypoints, legacy) == unclassified
    moved = next(iter(legacy))
    assert not root_layout_errors(actual - {moved}, entrypoints, legacy - {moved})
    assert not root_layout_errors(actual | {"reviewed_cli.py"}, entrypoints | {"reviewed_cli.py"}, legacy)


@pytest.mark.parametrize("source", ["franklin-street", "lyon-stahl", "colliers"])
def test_partial_readback_is_source_bound_and_rejects_foreign_target(tmp_path, source):
    run = tmp_path / RUN_ID
    if source == "colliers":
        child = colliers_child(run)
    else:
        child = add_franklin(lyon_child(run), run)
    checkpoint = child["sources"][source]
    key = {"franklin-street": "franklin_partial_readback",
           "lyon-stahl": "lyon_partial_readback",
           "colliers": "colliers_partial_readback"}[source]
    report = {key: copy.deepcopy(checkpoint["readback"]["observation"])}
    assert partial.partial_member_report_matches(report, checkpoint, source, RUN_ID) is True
    report[key]["database_target_sha256"] = "foreign"
    assert partial.partial_member_report_matches(report, checkpoint, source, RUN_ID) is False
    assert partial.partial_member_report_matches(report, checkpoint, "different-source", RUN_ID) is None
