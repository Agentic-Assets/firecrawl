#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

resolve_fc_dir() {
  if [[ -n "${FC_DIR:-}" ]]; then
    printf '%s\n' "$FC_DIR"
    return
  fi

  local from_script
  from_script="$(cd "$SCRIPT_DIR/../.." && pwd)"
  if [[ -f "$from_script/docker-compose.yaml" && -d "$from_script/.agents/skills" ]]; then
    printf '%s\n' "$from_script"
    return
  fi

  if [[ -f "$PWD/docker-compose.yaml" && -d "$PWD/.agents/skills" ]]; then
    printf '%s\n' "$PWD"
    return
  fi

  local common="$HOME/Documents/GitHub/agentic-assets/firecrawl"
  if [[ -f "$common/docker-compose.yaml" && -d "$common/.agents/skills" ]]; then
    printf '%s\n' "$common"
    return
  fi

  echo "Could not find the Firecrawl repo. Set FC_DIR=/path/to/firecrawl and rerun." >&2
  exit 1
}

FC_DIR="$(resolve_fc_dir)"
SOURCE_ROOT="${SOURCE_ROOT:-$FC_DIR/.agents/skills}"
TARGET_ROOT="${FIRECRAWL_USER_SKILLS_ROOT:-$HOME/.agents/skills}"
LINK_ROOTS_RAW="${FIRECRAWL_SKILL_LINK_ROOTS:-$HOME/.codex/skills:$HOME/.claude/skills:$HOME/.cursor/skills}"
SKILLS=(firecrawl-ops firecrawl-local-api)
DRY_RUN=0
FORCE=0

usage() {
  cat <<'EOF'
Usage: scripts/firecrawl-ops/sync_agent_skills.sh [--dry-run] [--force] [skill ...]

Copies canonical repo Firecrawl skills into ~/.agents/skills, then symlinks them
into user-level agent skill directories such as ~/.codex/skills, ~/.claude/skills,
and ~/.cursor/skills.

Env:
  FIRECRAWL_USER_SKILLS_ROOT   Canonical copy destination (default: ~/.agents/skills)
  FIRECRAWL_SKILL_LINK_ROOTS   Colon-separated symlink roots
                               (default: ~/.codex/skills:~/.claude/skills:~/.cursor/skills)
  FC_DIR                       Repo root override

Notes:
  - Repo skill symlinks are dereferenced during copy so ~/.agents/skills is standalone.
  - Existing non-symlink destinations are skipped unless --force is passed.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --force)
      FORCE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      SKILLS=("$@")
      break
      ;;
  esac
done

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '+ '
    printf '%q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

copy_skill() {
  local skill="$1"
  local src="$SOURCE_ROOT/$skill"
  local dst="$TARGET_ROOT/$skill"

  if [[ "$skill" == *"/"* || "$skill" == "." || "$skill" == ".." ]]; then
    echo "Invalid skill name: $skill" >&2
    exit 2
  fi
  if [[ ! -f "$src/SKILL.md" ]]; then
    echo "Missing skill source: $src/SKILL.md" >&2
    exit 1
  fi

  run mkdir -p "$TARGET_ROOT"
  run mkdir -p "$dst"
  run rsync -aL --delete "$src/" "$dst/"
  echo "Copied $skill -> $dst"
}

link_skill() {
  local skill="$1"
  local root="$2"
  local target="$TARGET_ROOT/$skill"
  local link="$root/$skill"

  run mkdir -p "$root"

  if [[ -e "$link" && ! -L "$link" ]]; then
    if [[ "$FORCE" == "1" ]]; then
      run rm -rf "$link"
    else
      echo "Skipped $link because it exists and is not a symlink. Re-run with --force to replace it." >&2
      return 0
    fi
  fi

  run ln -sfn "$target" "$link"
  echo "Linked $link -> $target"
}

IFS=':' read -r -a LINK_ROOTS <<< "$LINK_ROOTS_RAW"

for skill in "${SKILLS[@]}"; do
  copy_skill "$skill"
  for root in "${LINK_ROOTS[@]}"; do
    [[ -n "$root" ]] || continue
    link_skill "$skill" "$root"
  done
done

echo "Done. Canonical user-level skills live in $TARGET_ROOT"
