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

  local candidate
  for candidate in \
    "$HOME/Github/agentic-assets/firecrawl" \
    "$HOME/Documents/GitHub/agentic-assets/firecrawl"
  do
    if [[ -f "$candidate/docker-compose.yaml" && -d "$candidate/.agents/skills" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  done

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
  printf '%s\n' \
    'Usage: scripts/firecrawl-ops/sync_agent_skills.sh [--dry-run] [--force] [skill ...]' \
    '' \
    'Copies canonical repo Firecrawl skills into ~/.agents/skills, then symlinks them' \
    'into user-level agent skill directories such as ~/.codex/skills, ~/.claude/skills,' \
    'and ~/.cursor/skills.' \
    '' \
    'Env:' \
    '  FIRECRAWL_USER_SKILLS_ROOT   Canonical copy destination (default: ~/.agents/skills)' \
    '  FIRECRAWL_SKILL_LINK_ROOTS   Colon-separated symlink roots' \
    '                               (default: ~/.codex/skills:~/.claude/skills:~/.cursor/skills)' \
    '  FC_DIR                       Repo root override' \
    '' \
    'Notes:' \
    '  - Repo skill symlinks are dereferenced during copy so ~/.agents/skills is standalone.' \
    '  - Without rsync, a staged cp -RL replacement removes stale managed files.' \
    '  - Existing non-symlink destinations are skipped unless --force is passed.'
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

copy_without_rsync() {
  local src="$1" dst="$2" skill="$3"
  local managed_root stage
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '+ stage cp -RL %q then replace managed directory %q\n' "$src/." "$dst"
    return
  fi

  managed_root="$(cd "$TARGET_ROOT" && pwd -P)"
  [[ "$managed_root" != "/" && -n "$managed_root" ]] || {
    echo "Refusing unsafe skill installation root: $managed_root" >&2
    return 1
  }
  dst="$managed_root/$skill"
  if [[ -e "$dst" && ! -d "$dst" && ! -L "$dst" ]]; then
    echo "Skill destination is not a directory: $dst" >&2
    return 1
  fi
  stage="$(mktemp -d "$managed_root/.firecrawl-skill.XXXXXX")"
  # Recursive cleanup is confined to this newly created private staging
  # directory. Never resolve or traverse a pre-existing destination symlink.
  [[ "$(cd "$(dirname "$stage")" && pwd -P)" == "$managed_root" \
     && "$stage" == "$managed_root"/.firecrawl-skill.* && ! -L "$stage" ]] || {
    echo "Refusing unsafe skill staging path: $stage" >&2
    return 1
  }
  if ! mkdir "$stage/new" || ! cp -RL "$src/." "$stage/new/"; then
    rm -rf -- "$stage"
    echo "Skill copy failed; existing installation preserved: $dst" >&2
    return 1
  fi
  if [[ -e "$dst" || -L "$dst" ]]; then
    if ! mv "$dst" "$stage/previous"; then
      rm -rf -- "$stage"
      return 1
    fi
  fi
  if ! mv "$stage/new" "$dst"; then
    if [[ -e "$stage/previous" || -L "$stage/previous" ]]; then
      mv "$stage/previous" "$dst" || {
        echo "Restore failed; preserved previous skill at $stage/previous" >&2
        return 1
      }
    fi
    rm -rf -- "$stage"
    return 1
  fi
  rm -rf -- "$stage"
}

copy_skill() {
  local skill="$1"
  local src="$SOURCE_ROOT/$skill"
  local dst="$TARGET_ROOT/$skill"

  if [[ ! "$skill" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]]; then
    echo "Invalid skill name: $skill" >&2
    exit 2
  fi
  if [[ ! -f "$src/SKILL.md" ]]; then
    echo "Missing skill source: $src/SKILL.md" >&2
    exit 1
  fi

  run mkdir -p "$TARGET_ROOT"
  if ! command -v rsync >/dev/null 2>&1; then
    copy_without_rsync "$src" "$dst" "$skill"
    echo "Copied $skill -> $dst (cp fallback)"
    return
  fi
  # The installed copy must be a real directory. Otherwise rsync --delete
  # follows a pre-existing destination symlink and can modify its target.
  if [[ -L "$dst" ]]; then
    run rm "$dst"
  fi
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
