#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FC_DIR="${FC_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

cd "$FC_DIR"

if [[ ! -d .git ]]; then
  echo "Not a git checkout: $FC_DIR" >&2
  exit 1
fi

if [[ ! -x .githooks/post-commit || ! -x .githooks/pre-push ]]; then
  chmod +x .githooks/post-commit .githooks/pre-push
fi

git config core.hooksPath .githooks

echo "Enabled repo git hooks for $FC_DIR"
echo "core.hooksPath=$(git config --get core.hooksPath)"
echo "Hooks are advisory reminders only; they do not block commits or pushes."
