"""Read-only layout admission for collector tests, before runtime imports.

This cannot prove no-hardlinks provenance or that a checkout is unused by a
runtime. SETUP's separate-clone requirement still applies.
"""

import os
import stat
import subprocess
from pathlib import Path


class CheckoutAdmissionError(Exception):
    """The source checkout cannot safely supply a collector-local lock path."""


def admitted_lock_path(collector_dir: Path) -> Path:
    """Require ordinary, local Git metadata and unredirected collector paths.

    Preserve the inherited Git environment: production's resolver uses it too.
    Inspect only path topology, never lock contents or authority ownership.
    """
    try:
        source = Path(os.path.abspath(collector_dir))
        if source.parts[-3:] != ("scripts", "firecrawl-ops", "cre_collector"):
            raise CheckoutAdmissionError("unexpected collector source layout")
        root = source.parents[2].resolve(strict=True)
        collector = root / "scripts" / "firecrawl-ops" / "cre_collector"
        for path in (root / "scripts", collector.parent, collector):
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise CheckoutAdmissionError("collector source contains a redirect")
        if source.resolve(strict=True) != collector:
            raise CheckoutAdmissionError("collector source escapes its checkout")

        metadata = root / ".git"
        if not stat.S_ISDIR(metadata.lstat().st_mode):
            raise CheckoutAdmissionError(".git must be a local directory, not a worktree gitfile or redirect")
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-dir", "--git-common-dir"],
            cwd=collector,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        records = result.stdout.splitlines()
        if len(records) != 3 or any(not record or not Path(record).is_absolute() for record in records):
            raise CheckoutAdmissionError("Git returned ambiguous checkout metadata")
        top, git_dir, common_dir = (Path(record).resolve(strict=True) for record in records)
        if top != root or git_dir != metadata or common_dir != metadata:
            raise CheckoutAdmissionError("Git metadata does not belong to this ordinary checkout")

        lock = collector / "out" / "daily" / ".cre.lock"
        for path in (collector / "out", lock.parent, lock, lock.with_name(".cre.lock.authority")):
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                raise CheckoutAdmissionError("collector lock paths contain a symlink redirect")
            if path in (collector / "out", lock.parent) and not stat.S_ISDIR(mode):
                raise CheckoutAdmissionError("collector output parent is not a directory")
        return lock
    except CheckoutAdmissionError:
        raise
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        # Avoid echoing Git stderr/config or environment values into startup logs.
        raise CheckoutAdmissionError(f"cannot establish checkout layout ({type(exc).__name__})") from exc
