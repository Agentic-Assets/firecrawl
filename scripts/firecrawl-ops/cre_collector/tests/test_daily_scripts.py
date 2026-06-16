"""
test_daily_scripts.py

Behavioral contract for the disk-management shell helpers added in the
robustness wave (2026-06-14):

  - prune_keep()   in cre_daily_update.sh   (keeps newest N daily artifacts)
  - _keep_newest() in launchd/cre_run_tier.sh (bounds monitor enumeration
    artifacts, the ~100MB/run, 8x/day disk-fill risk)

test_shell_scripts_syntax.py only runs `bash -n` (a parse check); these tests
exercise the actual logic. They extract the real function source from each
script (so the test cannot drift from a copy), run it in a tmp directory, and
assert on the result. The key safety invariant: the run_*.json glob must never
delete the last_run_<tier>.json verdict markers that cre_status.sh reads.

Pure-local: tmp_path only, no network, no database. Skips if bash is absent.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

COLLECTOR = Path(__file__).resolve().parent.parent
DAILY = COLLECTOR / "cre_daily_update.sh"
RUN_TIER = COLLECTOR / "launchd" / "cre_run_tier.sh"
STATUS = COLLECTOR / "cre_status.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


def _extract_function(name, script_text):
    """Pull a top-level `name() { ... }` block from a shell script so the test
    exercises the real source. Relies on the closing brace sitting alone in
    column 0 (the style used in these scripts)."""
    lines = script_text.splitlines()
    start = None
    pat = re.compile(rf"^{re.escape(name)}\(\)\s*\{{")
    for i, line in enumerate(lines):
        if pat.match(line):
            start = i
            break
    assert start is not None, f"{name}() not found in script"
    for j in range(start + 1, len(lines)):
        if lines[j] == "}":
            return "\n".join(lines[start : j + 1])
    raise AssertionError(f"closing brace for {name}() not found")


def _mk_files(directory, names, base_mtime=1_700_000_000):
    directory.mkdir(parents=True, exist_ok=True)
    for offset, name in enumerate(names):
        f = directory / name
        f.write_text("x", encoding="utf-8")
        os.utime(f, (base_mtime + offset, base_mtime + offset))


def _run_prune_keep(out_dir, pattern, keep):
    func = _extract_function("prune_keep", DAILY.read_text(encoding="utf-8"))
    script = f'set -uo pipefail\nOUT_DIR="$1"\n{func}\nprune_keep "$2" "$3"\n'
    subprocess.run(
        ["bash", "-c", script, "bash", str(out_dir), pattern, str(keep)], check=True
    )


def _run_keep_newest(target_dir, pattern, keep):
    func = _extract_function("_keep_newest", RUN_TIER.read_text(encoding="utf-8"))
    script = f'set -uo pipefail\n{func}\n_keep_newest "$1" "$2" "$3"\n'
    subprocess.run(
        ["bash", "-c", script, "bash", str(target_dir), pattern, str(keep)], check=True
    )


def test_prune_keep_retains_newest_n_and_spares_markers(tmp_path):
    _mk_files(tmp_path, [f"run_2026-06-14_00{n:02d}.json" for n in range(20)])
    for tier in ("monitor", "daily", "weekly"):
        (tmp_path / f"last_run_{tier}.json").write_text("{}", encoding="utf-8")

    _run_prune_keep(tmp_path, "run_*.json", 14)

    runs = sorted(p.name for p in tmp_path.glob("run_*.json"))
    markers = sorted(tmp_path.glob("last_run_*.json"))
    assert len(runs) == 14, f"expected 14 kept, got {len(runs)}"
    assert len(markers) == 3, "verdict markers must survive the run_*.json glob"
    # Newest (highest mtime) retained, oldest pruned.
    assert "run_2026-06-14_0019.json" in runs
    assert "run_2026-06-14_0000.json" not in runs


def test_prune_keep_noop_when_at_or_under_threshold(tmp_path):
    _mk_files(tmp_path, [f"run_2026-06-14_00{n:02d}.json" for n in range(3)])
    _run_prune_keep(tmp_path, "run_*.json", 14)
    assert len(list(tmp_path.glob("run_*.json"))) == 3


def test_prune_keep_tolerates_spaces_in_out_dir(tmp_path):
    spaced = tmp_path / "with space"
    _mk_files(spaced, [f"run_2026-06-14_00{n:02d}.json" for n in range(18)])
    _run_prune_keep(spaced, "run_*.json", 14)
    assert len(list(spaced.glob("run_*.json"))) == 14


def test_keep_newest_bounds_monitor_artifacts(tmp_path):
    _mk_files(tmp_path, [f"monitor_2026-06-14_00{n:02d}.json" for n in range(30)])
    _run_keep_newest(tmp_path, "monitor_*.json", 24)
    kept = sorted(p.name for p in tmp_path.glob("monitor_*.json"))
    assert len(kept) == 24, f"expected 24 monitor artifacts kept, got {len(kept)}"
    assert "monitor_2026-06-14_0029.json" in kept
    assert "monitor_2026-06-14_0000.json" not in kept


def test_cre_status_reports_daily_while_legacy_tier_is_live():
    text = STATUS.read_text(encoding="utf-8")
    assert "for tier in monitor enrich daily weekly" in text
    assert "last_run_$tier.json" in text
    assert "ai.agentic.cre-daily.plist" in text


def test_cre_status_flags_empty_or_malformed_markers():
    text = STATUS.read_text(encoding="utf-8")
    assert "marker_problem" in text
    assert "empty marker:" in text
    assert "malformed marker:" in text


def test_cre_status_derives_disappearance_only_sources_from_ingest_contract():
    text = STATUS.read_text(encoding="utf-8")
    assert "STATUS_SOURCE_PATHS" in text
    assert "if not paths" in text
    assert (
        'DISAPPEAR_ONLY_SOURCES="avison-young cbre jll marcus-millichap '
        'newmark savills transwestern"'
    ) in text
