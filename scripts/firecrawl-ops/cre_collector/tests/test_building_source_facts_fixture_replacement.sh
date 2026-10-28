#!/bin/bash
# Fake Docker/uuidgen test: no real container is touched.
set -euo pipefail
test_dir="$(cd "$(dirname "$0")" && pwd)"
test_root="$(mktemp -d "${TMPDIR:-/tmp}/cre-building-replacement.XXXXXX")"
trap 'rm -rf "$test_root"' EXIT
mkdir "$test_root/bin"
cat > "$test_root/bin/uuidgen" <<'MOCK'
#!/bin/sh
printf '%s\n' '11111111-1111-4111-8111-111111111111'
MOCK
cat > "$test_root/bin/sleep" <<'MOCK'
#!/bin/sh
exit 0
MOCK
cat > "$test_root/bin/docker" <<'MOCK'
#!/bin/sh
original=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
foreign=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
name=cre-building-facts-11111111-1111-4111-8111-111111111111
token=11111111-1111-4111-8111-111111111111
case "$1" in
  run) printf '%s\n' "$original" ;;
  inspect)
    for arg do target="$arg"; done
    if [ "$target" = "$original" ]; then printf '%s /%s %s\n' "$original" "$name" "$token";
    else printf '%s /%s %s\n' "$foreign" "$name" 'foreign-token'; fi ;;
  exec)
    shift
    if [ "$1" = '-i' ]; then shift; fi
    printf '%s\n' "$1" >> "$FIXTURE_FAKE_EXEC_LOG"
    exit 1 ;;
  rm) for arg do target="$arg"; done; printf '%s\n' "$target" > "$FIXTURE_FAKE_RM_LOG" ;;
  *) exit 2 ;;
esac
MOCK
chmod +x "$test_root/bin/uuidgen" "$test_root/bin/sleep" "$test_root/bin/docker"
if PATH="$test_root/bin:$PATH" FIXTURE_FAKE_EXEC_LOG="$test_root/exec" FIXTURE_FAKE_RM_LOG="$test_root/rm" bash "$test_dir/run_building_source_facts_postgres_contract.sh" > "$test_root/output" 2>&1; then
 echo 'FAIL fake unavailable original unexpectedly passed'; exit 1
fi
original=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
[[ -s "$test_root/exec" ]] || { echo 'FAIL no exec attempted'; exit 1; }
if grep -vx "$original" "$test_root/exec" >/dev/null; then echo 'FAIL exec used mutable/replaced name'; exit 1; fi
[[ "$(cat "$test_root/rm")" == "$original" ]] || { echo 'FAIL cleanup did not target original ID'; exit 1; }
echo 'PASS mutable-name replacement cannot redirect exec or cleanup'
