#!/usr/bin/env python3
"""CI-safe guard for API pnpm and Docker native-dependency assumptions."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


DEFAULT_API_DIR = Path("apps/api")
FORBIDDEN_PACKAGE_PNPM_KEYS = {
    "overrides",
    "onlyBuiltDependencies",
    "patchedDependencies",
    "allowedDeprecatedVersions",
    "allowBuilds",
}
REQUIRED_WORKSPACE_SECTIONS = ("overrides:", "onlyBuiltDependencies:", "patchedDependencies:")
REQUIRED_NATIVE_DEPS = ("foundationdb", "libpq")


def fail(messages: list[str], message: str) -> None:
    messages.append(message)


def load_package_json(api_dir: Path) -> dict:
    return json.loads((api_dir / "package.json").read_text(encoding="utf-8"))


def section_contains_list_item(text: str, section_name: str, item: str) -> bool:
    pattern = re.compile(rf"^{re.escape(section_name)}:\n(?P<body>(?:  .+\n)+)", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return False
    return re.search(rf"^\s*-\s+{re.escape(item)}\s*$", match.group("body"), re.MULTILINE) is not None


def dockerfile_has_fdb_before_pnpm_install(text: str) -> bool:
    fdb_index = text.find("foundationdb-clients_${FDB_VERSION}")
    pnpm_index = text.find("pnpm install --frozen-lockfile")
    return fdb_index != -1 and pnpm_index != -1 and fdb_index < pnpm_index


def run_checks(api_dir: Path) -> list[str]:
    errors: list[str] = []
    package_json = load_package_json(api_dir)
    workspace_path = api_dir / "pnpm-workspace.yaml"
    dockerfile_path = api_dir / "Dockerfile"
    workspace_text = workspace_path.read_text(encoding="utf-8") if workspace_path.is_file() else ""
    dockerfile_text = dockerfile_path.read_text(encoding="utf-8") if dockerfile_path.is_file() else ""

    pnpm_config = package_json.get("pnpm", {})
    if not isinstance(pnpm_config, dict):
        fail(errors, "package.json pnpm config must be an object")
    else:
        forbidden_present = sorted(FORBIDDEN_PACKAGE_PNPM_KEYS.intersection(pnpm_config))
        if forbidden_present:
            fail(
                errors,
                "workspace-level pnpm keys must stay in apps/api/pnpm-workspace.yaml, not package.json: "
                + ", ".join(forbidden_present),
            )

    package_manager = package_json.get("packageManager", "")
    if package_manager != "pnpm@10.16.1":
        fail(errors, f"packageManager should remain pnpm@10.16.1, got {package_manager!r}")

    if not workspace_path.is_file():
        fail(errors, "apps/api/pnpm-workspace.yaml is missing")
    for section in REQUIRED_WORKSPACE_SECTIONS:
        if section not in workspace_text:
            fail(errors, f"pnpm-workspace.yaml is missing {section}")
    for dependency in REQUIRED_NATIVE_DEPS:
        if not section_contains_list_item(workspace_text, "onlyBuiltDependencies", dependency):
            fail(errors, f"onlyBuiltDependencies must include {dependency}")
    if re.search(r"allowBuilds:\s*\n(?:\s+.+\n)*\s+foundationdb:\s*true", workspace_text):
        fail(errors, "pnpm-workspace.yaml must not force host allowBuilds.foundationdb=true")
    if re.search(r"allowBuilds:\s*\n(?:\s+.+\n)*\s+libpq:\s*true", workspace_text):
        fail(errors, "pnpm-workspace.yaml must not force host allowBuilds.libpq=true")

    if not dockerfile_path.is_file():
        fail(errors, "apps/api/Dockerfile is missing")
    if "build-essential" not in dockerfile_text or "pkg-config" not in dockerfile_text or "python3" not in dockerfile_text:
        fail(errors, "Dockerfile build stage must install build-essential, pkg-config, and python3 before pnpm install")
    if not dockerfile_has_fdb_before_pnpm_install(dockerfile_text):
        fail(errors, "Dockerfile must install FoundationDB clients before pnpm install")
    if "pnpm install --frozen-lockfile" not in dockerfile_text:
        fail(errors, "Dockerfile must use pnpm install --frozen-lockfile")
    if "pnpm prune --prod --ignore-scripts" not in dockerfile_text:
        fail(errors, "Dockerfile must prune production dependencies with --ignore-scripts")

    return errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-dir", type=Path, default=DEFAULT_API_DIR)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    errors = run_checks(args.api_dir)
    if errors:
        print("pnpm/Docker config guard failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("pnpm/Docker config guard passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
