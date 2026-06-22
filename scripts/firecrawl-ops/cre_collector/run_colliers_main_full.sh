#!/usr/bin/env bash
# Drive the full colliers-main enrichment in bounded chunks.
#
# Why: each detail render leaks ~0.8 MB in the fetch/SDK layer, so a single
# unbounded ~15.9k-URL run exhausts the V8 heap (~4 GB) and aborts mid-run. This
# driver runs the collector in chunks capped at COLLIERS_MAIN_MAX_FETCHES_PER_RUN
# new fetches each; every chunk is a fresh process that frees all memory on exit.
# The durable JSONL cache (out/cache/colliers-main/detail-cache.jsonl) lets each
# chunk resume where the last stopped. When the cache stops growing and a chunk
# reports 0 deferred URLs, the run has converged and the final chunk (all cache
# hits, zero fetches) has written the complete artifact.
#
# Usage: bash run_colliers_main_full.sh
# Tunables: CAP (fetches/chunk), HEAP_MB, CONC, MAX_CHUNKS, OUT.
set -uo pipefail
cd "$(dirname "$0")"

CAP="${CAP:-4000}"
HEAP_MB="${HEAP_MB:-6144}"
CONC="${CONC:-2}"
MAX_CHUNKS="${MAX_CHUNKS:-10}"
OUT="${OUT:-out/colliers_main_full_2026-06-13.json}"
CACHE="out/cache/colliers-main/detail-cache.jsonl"
LOG="out/daily/colliers_main_driver_2026-06-13.log"
mkdir -p out/daily

echo "=== colliers-main driver start $(date -u +%FT%TZ): cap=$CAP heap=${HEAP_MB}MB conc=$CONC ===" | tee -a "$LOG"

for i in $(seq 1 "$MAX_CHUNKS"); do
  before=$( [ -f "$CACHE" ] && wc -l < "$CACHE" || echo 0 )
  chunklog="out/daily/colliers_main_chunk_${i}_2026-06-13.log"
  echo "=== chunk $i start $(date -u +%FT%TZ): cache=$before ===" | tee -a "$LOG"

  COLLIERS_MAIN_DETAIL_CONCURRENCY="$CONC" \
  COLLIERS_MAIN_MAX_FETCHES_PER_RUN="$CAP" \
  NODE_OPTIONS="--max-old-space-size=${HEAP_MB}" \
    npx tsx collect.ts --source=colliers-main --transaction=both \
      --max-items=0 --out="$OUT" > "$chunklog" 2>&1
  rc=$?

  after=$( wc -l < "$CACHE" )
  deferred=$( grep -oE '[0-9]+ URL\(s\) deferred' "$chunklog" | tail -1 | grep -oE '^[0-9]+' || echo 0 )
  echo "=== chunk $i done $(date -u +%FT%TZ): rc=$rc cache ${before}->${after} deferred=${deferred:-0} ===" | tee -a "$LOG"
  tail -4 "$chunklog" | tee -a "$LOG"

  if [ "$after" -le "$before" ] && [ "${deferred:-0}" -eq 0 ]; then
    echo "=== converged at chunk $i: cache complete, artifact at $OUT ===" | tee -a "$LOG"
    break
  fi
done

echo "=== colliers-main driver complete $(date -u +%FT%TZ): cache rows=$( wc -l < "$CACHE" ), artifact=$OUT ===" | tee -a "$LOG"
