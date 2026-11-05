"""Exercise canonical owner identity through the real checkpoint script entrypoint."""
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("module_mode", [False, True])
def test_script_entrypoint_uses_one_owner_and_exception_identity(tmp_path, module_mode):
    collector = Path(__file__).resolve().parents[1]
    hook_dir = tmp_path / "hook"
    hook_dir.mkdir()
    (hook_dir / "sitecustomize.py").write_text('''
import os
import sys
from pathlib import Path

if "--help" in sys.argv:
    def trace(frame, event, arg):
        if (event != "call" or frame.f_code.co_name != "main"
                or Path(frame.f_globals.get("__file__", "")).name != "cre_checkpoint_refresh.py"):
            return trace
        sys.settrace(None)
        import cre_checkpoint_refresh as refresh
        import cre_refresh_pipeline as pipeline
        from cre_colliers_partial_checkpoint import run_window_worker
        from tests.test_coordinator_clock_ordering import (
            test_real_colliers_source_recovers_after_dispatch_clock_reversal,
        )
        from pytest import MonkeyPatch

        assert refresh is sys.modules["__main__"]
        assert pipeline.run_bounded_pipeline.__globals__["__name__"] == "cre_refresh_pipeline"
        try:
            raise sys.modules["__main__"].SourceGuardRefusal("synthetic")
        except refresh.SourceGuardRefusal:
            pass
        else:
            raise AssertionError("exception identity differs")
        try:
            run_window_worker(Path(os.environ["IDENTITY_FIXTURE_ROOT"]), {}, object(),
                              lambda *a, **k: 0, lambda *a, **k: None)
        except TypeError as exc:
            assert str(exc) == "partial worker owner invalid"
        else:
            raise AssertionError("forged owner accepted")
        with MonkeyPatch.context() as patcher:
            test_real_colliers_source_recovers_after_dispatch_clock_reversal(
                Path(os.environ["IDENTITY_FIXTURE_ROOT"]), patcher)
        print("CLI_OWNER_AND_WORKER_VERIFIED")
        raise SystemExit(0)
    sys.settrace(trace)
''')
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(hook_dir), str(collector))),
           "IDENTITY_FIXTURE_ROOT": str(fixture_root)}
    command = ([sys.executable, "-m", "cre_checkpoint_refresh", "--help"] if module_mode
               else [sys.executable, str(collector / "cre_checkpoint_refresh.py"), "--help"])
    result = subprocess.run(
        command,
        cwd=collector, env=env, text=True, capture_output=True, timeout=180, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CLI_OWNER_AND_WORKER_VERIFIED" in result.stdout
