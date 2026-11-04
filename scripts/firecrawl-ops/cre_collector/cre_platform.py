"""Supported POSIX runtime boundary for the CRE collector.

Native Windows cannot supply the collector's flock/process-group authority.
Use Ubuntu in WSL2 rather than emulating or disabling those primitives.
"""

from __future__ import annotations

import sys

UNSUPPORTED_MESSAGE = (
    "CRE collector requires Linux/WSL2 or macOS. Native Windows is unsupported; "
    "run this command with Linux Python in Ubuntu WSL2 (not Git Bash Python). "
    "POSIX locks and process groups must not be bypassed."
)


def require_supported_platform(platform: str | None = None) -> None:
    if (sys.platform if platform is None else platform) not in {"linux", "darwin"}:
        raise SystemExit(UNSUPPORTED_MESSAGE)


if __name__ == "__main__":
    require_supported_platform()
