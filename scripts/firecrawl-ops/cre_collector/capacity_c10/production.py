"""The sole production C10 arm entrypoint."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .contracts import C10Error, validate_plan
from .host_session import (
    C10HostExecutionSession,
    C10SealedCardRegistry,
    C10SessionStore,
)


def execute_production_arm(
    *,
    repo_root: Path,
    plan: Mapping[str, Any],
    cohort: Mapping[str, Any],
    session: Mapping[str, Any],
    session_store_path: Path,
    private_root: Path,
    timeout_seconds: float = 120,
) -> Mapping[str, Any]:
    """Execute only the next sealed arm; no browser/scheduler injection exists."""
    validate_plan(plan)
    host = C10HostExecutionSession(
        repo_root=repo_root,
        session_store=C10SessionStore(session_store_path),
        private_root=private_root,
        cards=C10SealedCardRegistry(plan, cohort),
    )
    return host.execute(plan, session, timeout_seconds=timeout_seconds)


def _read(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise C10Error(f"C10 {label} JSON cannot be read") from exc
    if not isinstance(value, Mapping):
        raise C10Error(f"C10 {label} JSON must be an object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execute one sealed C10 host arm")
    parser.add_argument("--execute", action="store_true", help="required; no dry-run")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--session-store", type=Path, required=True)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    args = parser.parse_args(argv)
    if not args.execute:
        raise C10Error("C10 production CLI refuses dry-run or implicit execution")
    execute_production_arm(
        repo_root=args.repo_root.resolve(),
        plan=_read(args.plan, "plan"),
        cohort=_read(args.cohort, "cohort"),
        session=_read(args.session, "session"),
        session_store_path=args.session_store.resolve(),
        private_root=args.private_root.resolve(),
        timeout_seconds=args.timeout_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
