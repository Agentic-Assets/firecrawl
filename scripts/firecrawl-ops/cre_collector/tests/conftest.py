"""
conftest.py: put the parent cre_collector dir on sys.path so
`from cre_ingest import ...` resolves correctly under pytest, regardless of
where pytest is invoked from.

This file also owns a session-wide safety net around the REAL canonical CRE
lock at out/daily/.cre.lock (+ its .authority sidecar). See
LOCK_AUTHORITY_RECOVERY.md and the 2026-09-15 incident it documents: a test
that failed to inject a tmp_path lock resolved the production
`canonical_shared_lock_dir()` default and left quarantine markers
(capacity-benchmark-active.json / capacity-benchmark-quarantine.json) inside
the real lock directory, which then blocked live collector runs until an
operator ran the governed `cre_capacity_runtime.py recover-quarantine` tool.

Tests must always inject an explicit tmp_path-based lock (either a
`_canonical_lock_path` / `canonical_lock_path` kwarg, or by monkeypatching the
module-local `canonical_shared_lock_dir` binding). The guard below is
defense-in-depth, not a substitute for that: it (1) fails loudly, from inside
the test, the first time any code under test would resolve the *real*
canonical lock path via the unpatched production resolver, and (2) as a
backstop that also catches leaks from spawned subprocesses (e.g. the
cre_tier_dispatch / cre_run_tier.sh child-process tests), verifies after every
single test that the real lock pair's on-disk state is byte-for-byte
unchanged, and fails the test loudly if it is not.
"""

import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

# Insert cre_collector/ at the front of sys.path so cre_ingest is importable
# without any package install step.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import cre_checkpoint_refresh as _refresh

# Captured once, at collection time, before any test can monkeypatch it. This
# is the one reference we trust to compute what the REAL canonical lock path
# is; every module-local `canonical_shared_lock_dir` binding discovered below
# is compared against this exact function object by identity, and every
# resolution funnelled back through it is checked against the real result.
_REAL_CANONICAL_SHARED_LOCK_DIR = _refresh.canonical_shared_lock_dir
_REAL_CANONICAL_LOCK_DIR = _REAL_CANONICAL_SHARED_LOCK_DIR(Path(_PARENT)).resolve()
_REAL_AUTHORITY_PATH = _REAL_CANONICAL_LOCK_DIR.with_name(
    f"{_REAL_CANONICAL_LOCK_DIR.name}.authority"
)


_UNREADABLE = "<unreadable-do-not-compare>"


def _lock_snapshot(path: Path) -> tuple[object, ...] | None:
    """Read-only contamination sentinel for one real lock-pair path.

    Never opens the path for writing and never deletes or moves it. For a
    directory this captures identity/size/mtime, which changes whenever an
    entry is added, removed, or renamed inside it (the actual shape of the
    2026-09-15 leak: quarantine marker files written inside the real
    out/daily/.cre.lock directory). For a regular file it also hashes the
    content so a same-size in-place rewrite is still detected.

    The (dev, ino, mode, size, mtime_ns) tuple from a real `lstat()` is the
    authoritative identity check -- it alone is sufficient to prove nothing
    was created, deleted, renamed, or rewritten. The content digest is a
    best-effort secondary check layered on top of that; if some unrelated
    test has monkeypatched low-level os/io primitives (observed with tests
    that simulate a closed fd for interrupt/CPU-guard cleanup races) and the
    read itself breaks, that must never be mistaken for a real content
    change, so an unreadable digest is a distinct sentinel that the
    comparator below treats as inconclusive rather than "changed".
    """
    try:
        observed = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    digest = None
    if stat.S_ISREG(observed.st_mode):
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception:  # noqa: BLE001 - see docstring
            digest = _UNREADABLE
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_mode,
        observed.st_size,
        observed.st_mtime_ns,
        digest,
    )


def _snapshots_indicate_a_real_change(
    before: tuple[object, ...] | None, after: tuple[object, ...] | None
) -> bool:
    """True only when the lock pair's real identity/content actually
    changed; an unreadable digest on either side never counts on its own."""
    if before is None or after is None:
        return before != after
    before_identity, before_digest = before[:5], before[5]
    after_identity, after_digest = after[:5], after[5]
    if before_identity != after_identity:
        return True
    if _UNREADABLE in (before_digest, after_digest):
        return False
    return before_digest != after_digest


def _guarded_canonical_shared_lock_dir(*args, **kwargs):
    """Drop-in stand-in for `canonical_shared_lock_dir` used under pytest.

    Delegates to the real resolver (so legitimate unit tests of the resolver
    itself, exercised against a tmp_path git checkout, still work unchanged)
    but fails loudly the moment the resolved path is the REAL production
    canonical lock directory. A test that needs the real production behavior
    is not something this suite should ever need; every caller must inject an
    explicit tmp_path lock instead.
    """
    result = _REAL_CANONICAL_SHARED_LOCK_DIR(*args, **kwargs)
    if Path(result).resolve() == _REAL_CANONICAL_LOCK_DIR:
        pytest.fail(
            "test resolved the REAL canonical CRE lock path "
            f"({_REAL_CANONICAL_LOCK_DIR}) via the unpatched "
            "canonical_shared_lock_dir() default. Every test must inject an "
            "explicit tmp_path-based lock path instead (a "
            "`_canonical_lock_path=` / `canonical_lock_path=` kwarg, or a "
            "monkeypatch of the module-local `canonical_shared_lock_dir` "
            "binding). See LOCK_AUTHORITY_RECOVERY.md for the incident this "
            "guards against."
        )
    return result


def _modules_bound_to_real_resolver():
    """Every currently-imported module whose `canonical_shared_lock_dir`
    attribute is still the exact, unpatched production function object.

    Iterates `sys.modules` rather than a hardcoded module list so a new
    module added later (another `from cre_checkpoint_refresh import
    canonical_shared_lock_dir`) is covered automatically without editing this
    file. Collection has already imported every test module (and therefore
    every production module they import at file scope) by the time any test
    runs, so this reliably finds cre_checkpoint_refresh itself plus every
    downstream importer (cre_capacity_benchmark, cre_capacity_runtime,
    cre_tier_dispatch, cre_enqueue_source_refresh, cre_repair_*,
    capacity_c10.host_orchestration, capacity_c10.production, ...).
    """
    for module in list(sys.modules.values()):
        if module is None:
            continue
        try:
            current = module.__dict__.get("canonical_shared_lock_dir")
        except Exception:  # noqa: BLE001, S112 - some modules have odd __dict__ proxies
            continue  # skip this module; the guard still covers every other one
        if current is _REAL_CANONICAL_SHARED_LOCK_DIR:
            yield module


@pytest.fixture(autouse=True)
def _never_touch_real_canonical_cre_lock(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
):
    """Session-wide guard: no test may resolve or mutate the real
    out/daily/.cre.lock (+ .authority sidecar). See module docstring.
    """
    before_primary = _lock_snapshot(_REAL_CANONICAL_LOCK_DIR)
    before_authority = _lock_snapshot(_REAL_AUTHORITY_PATH)

    for module in _modules_bound_to_real_resolver():
        monkeypatch.setattr(
            module,
            "canonical_shared_lock_dir",
            _guarded_canonical_shared_lock_dir,
            raising=True,
        )

    yield

    after_primary = _lock_snapshot(_REAL_CANONICAL_LOCK_DIR)
    after_authority = _lock_snapshot(_REAL_AUTHORITY_PATH)
    changed = _snapshots_indicate_a_real_change(
        before_primary, after_primary
    ) or _snapshots_indicate_a_real_change(before_authority, after_authority)
    assert not changed, (
        "the REAL canonical CRE lock pair "
        f"({_REAL_CANONICAL_LOCK_DIR}) changed during "
        f"{request.node.nodeid}. This must never happen under pytest. Do NOT "
        "delete, move, or otherwise touch the real lock/authority files as a "
        "workaround -- they are forensic evidence; recovery is only via the "
        "governed `cre_capacity_runtime.py recover-quarantine` tool. "
        "Investigate the failing test's lock-path injection instead."
    )


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
