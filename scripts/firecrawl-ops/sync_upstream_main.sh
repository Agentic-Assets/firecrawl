#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FC_DIR="${FC_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
DATE_STAMP="${DATE_STAMP:-$(date +%F)}"
BRANCH="${1:-codex/sync-upstream-$DATE_STAMP}"
PROTECTED_PATHS=(
  ".agents"
  "docs/firecrawl-ops"
  "scripts/firecrawl-ops"
  "LOCAL_DEVELOPMENT_GUIDE.md"
  "AGENTS.md"
)

cd "$FC_DIR"

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Working tree has local changes. Commit/stash them before syncing upstream." >&2
  exit 1
fi

if ! git remote get-url upstream >/dev/null 2>&1; then
  git remote add upstream https://github.com/firecrawl/firecrawl.git
fi

git fetch upstream main --prune
git fetch origin main --prune
git switch main
git switch -c "$BRANCH"
git merge --no-ff upstream/main -m "chore: sync upstream firecrawl/main ($DATE_STAMP)"

echo
echo "Protected fork path diff from main...HEAD:"
git diff --name-status main...HEAD -- "${PROTECTED_PATHS[@]}" || true
echo
echo "Next:"
echo "  run focused API/SDK tests"
echo "  git push -u origin $BRANCH"
echo "  open a PR into Agentic-Assets/firecrawl:main"
