"""Check or restore the repository's canonical relative skill symlinks.

Windows requires Developer Mode or an elevated Python process. This helper never
falls back to copies or junctions, and never changes Git's tracked link blobs.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_LINKS = {
    ".claude/skills": "../.agents/skills",
    ".agents/skills/firecrawl-ops/scripts": "../../../scripts/firecrawl-ops",
    ".agents/skills/firecrawl-ops/references": "../../../docs/firecrawl-ops/references",
    ".agents/skills/firecrawl-local-api/scripts": "../../../scripts/firecrawl-ops",
    ".agents/skills/firecrawl-local-api/references": "../../../docs/firecrawl-ops/references",
}


def inspect_skill_links(root: Path = REPO_ROOT) -> dict[str, str]:
    """Report only allowlisted paths, without following an unexpected link."""
    result = {}
    for relative, target in SKILL_LINKS.items():
        path = root / relative
        if path.is_symlink():
            if os.readlink(path).replace("\\", "/") != target:
                result[relative] = "unexpected"
            else:
                result[relative] = "ready" if path.is_dir() else "broken"
        elif not path.exists():
            result[relative] = "missing"
        elif path.is_file() and path.stat().st_size <= 128:
            result[relative] = (
                "placeholder"
                if path.read_bytes() == target.encode("utf-8")
                else "unexpected"
            )
        else:
            result[relative] = "unexpected"
    return result


def ensure_skill_links(root: Path = REPO_ROOT) -> dict[str, str]:
    """Replace exact Git placeholders only after all link creation succeeds."""
    root = root.resolve(strict=True)
    states = inspect_skill_links(root)
    if "unexpected" in states.values():
        raise ValueError("Unexpected skill path; refusing to replace user content.")
    for relative, target in SKILL_LINKS.items():
        path = root / relative
        if path.parent.resolve() != path.parent.absolute():
            raise ValueError("Skill link parent must not be redirected.")
        if not (path.parent / target).is_dir():
            raise ValueError(f"Missing skill link target: {relative}")
    staged: list[tuple[Path, Path, bytes | None, str | None]] = []
    try:
        for relative, state in states.items():
            if state == "ready":
                continue
            path = root / relative
            target = SKILL_LINKS[relative]
            backup = path.read_bytes() if state == "placeholder" else None
            old_link = os.readlink(path) if state == "broken" else None
            temporary = path.with_name(f".{path.name}-{uuid4().hex}.tmp")
            # Windows stores separators verbatim in relative reparse points.
            temporary.symlink_to(os.path.normpath(target), target_is_directory=True)
            staged.append((temporary, path, backup, old_link))
        for temporary, path, backup, old_link in staged:
            if backup is not None:
                # A directory symlink cannot replace a plain file on Windows.
                # Recheck the placeholder before removing it.
                if path.is_symlink() or path.read_bytes() != backup:
                    raise ValueError("Skill placeholder changed during repair.")
                path.unlink()
            elif old_link is not None:
                if not path.is_symlink() or os.readlink(path) != old_link:
                    raise ValueError("Skill link changed during repair.")
                path.unlink()
            try:
                temporary.rename(path)
            except OSError:
                if backup is not None and not path.exists():
                    path.write_bytes(backup)
                elif (
                    old_link is not None and not path.is_symlink() and not path.exists()
                ):
                    path.symlink_to(old_link, target_is_directory=True)
                raise
    finally:
        for temporary, _, _, _ in staged:
            if temporary.is_symlink():
                temporary.unlink()
    return inspect_skill_links(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Repair canonical links.")
    args = parser.parse_args(argv)
    try:
        states = ensure_skill_links() if args.apply else inspect_skill_links()
    except (OSError, ValueError) as exc:
        parser.exit(
            1,
            f"Skill link repair refused: {exc}\nOn Windows enable Developer Mode or run this helper in an administrator terminal.\n",
        )
    print(
        json.dumps(
            {"ready": all(s == "ready" for s in states.values()), "links": states},
            sort_keys=True,
        )
    )
    return 0 if all(state == "ready" for state in states.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
