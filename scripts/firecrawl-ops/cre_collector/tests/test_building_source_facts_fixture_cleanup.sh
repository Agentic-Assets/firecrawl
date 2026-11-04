#!/bin/bash
# Fake command regression: no real container is created or inspected.
set -euo pipefail
test_dir="$(cd "$(dirname "$0")" && pwd)"
test_root="$(mktemp -d "${TMPDIR:-/tmp}/cre-building-cleanup.XXXXXX")"
trap 'rm -rf "$test_root"' EXIT
mkdir "$test_root/bin"
cat > "$test_root/bin/uuidgen" <<'MOCK'
#!/bin/sh
printf '%s\n' '11111111-1111-4111-8111-111111111111'
MOCK
cat > "$test_root/bin/docker" <<'MOCK'
#!/bin/sh
case "$1" in
  run) exit 125 ;;
  inspect) printf '%064d %s\n' 0 'someone-elses-token' ;;
  rm) printf '%s\n' 'removed' > "$FIXTURE_FAKE_RM_MARKER"; exit 0 ;;
  *) exit 2 ;;
esac
MOCK
chmod +x "$test_root/bin/uuidgen" "$test_root/bin/docker"
if PATH="$test_root/bin:$PATH" FIXTURE_FAKE_RM_MARKER="$test_root/removed" bash "$test_dir/run_building_source_facts_postgres_contract.sh" > "$test_root/output" 2>&1; then
  echo 'FAIL name collision unexpectedly passed'; exit 1
fi
if [[ -e "$test_root/removed" ]]; then echo 'FAIL foreign container removed'; exit 1; fi
if ! grep -q 'ownership mismatch; left untouched' "$test_root/output"; then cat "$test_root/output"; exit 1; fi
echo 'PASS failed start with foreign same-name container does not remove it'
