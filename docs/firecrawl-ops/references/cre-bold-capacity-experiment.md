# CRE bold capacity experiment

`bold-jll-128` is a named, no-write experiment profile. It is not the default
production profile and normal collector startup never selects it. The current
checkpoint series remains serial: the later two-provider split is a planned
global 10-page allocation of 6 plus 4, not 10 pages per provider and not an
implemented parallelism setting.

First resolve the profile without reading `.env`, contacting Docker, or writing
an artifact:

```bash
cd scripts/firecrawl-ops/cre_collector
python3 cre_capacity_experiment.py --profile bold-jll-128
```

For an admitted execution, the runtime owner supplies a redaction-safe
effective-settings JSON containing only the five verified memory/VM fields.
It must match the recorded 32 GiB OrbStack, browser 16 GiB/no swap, and API
8 GiB/no swap baseline. Then write the restricted, replayable settings record:

```bash
python3 cre_capacity_experiment.py --profile bold-jll-128 \
  --effective-settings /restricted/runtime-effective.json \
  --write-plan /restricted/cre-bold-128/resolved-settings.json
```

The requested browser 6 CPU, global 10 pages, JLL width 10, browser PID 768,
and API 2 CPU settings remain proposed until technical admission. Do not use
`set_cre_resource_profile.sh apply`, recreate a container, or recreate the API
from the current `.env` as part of this preflight. The API's existing
environment fingerprint differs from that `.env`; recreating it could adopt an
unreviewed model/environment change.

The separate capacity benchmark adapter requires a matching technical-admission
record and predeclared 128-detail sample manifest. It must retain restricted raw,
native, and normalized artifacts, use the 90 percent for 30 seconds sampled
every 2 seconds guard, and stop for provider 429/challenge cooldown. Its output
is evidence only: the historical global duplicate-URL regression still blocks
any write canary. A safe negative or missing optional telemetry result is
inconclusive, not a production pipeline failure; identity, fidelity, freshness,
provenance, OOM/PID, and writer boundaries remain fail-closed.
