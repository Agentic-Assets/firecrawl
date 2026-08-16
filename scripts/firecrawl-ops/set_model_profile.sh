#!/usr/bin/env bash
set -euo pipefail

# Retired mutable entrypoint. Model routing changes are applied only by the
# explicit firecrawl_operator_handoff.py --apply --confirm workflow; this
# script deliberately ignores every argument and environment variable.

echo "Direct model-profile mutation is disabled." >&2
echo "Use firecrawl_operator_handoff.py model --profile <profile> for a dry-run plan," >&2
echo "then an explicitly attested --apply transition after the queue is idle." >&2
exit 2
