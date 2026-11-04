# CRE listings collector

This directory contains the active collector and ingestor for the shared CRE listing database. Use this map to find the right code; use fresh run receipts and scheduler readback for current operating status.

## Find the right area

| Work | Start here |
| --- | --- |
| Brokerage inventory and listing detail | [sources/](sources/) and [collect.ts](collect.ts); shared scraping and parsing live in [lib/](lib/) |
| Normalize and save listings | [cre_ingest.py](cre_ingest.py); existing listing validation is in [cre_validate.py](cre_validate.py) |
| Run a refresh on Windows | [guarded incremental tiers and Windows launcher](../../../docs/firecrawl-ops/references/guarded-incremental-tiers.md); the shared refresh and series entrypoints are [cre_checkpoint_refresh.py](cre_checkpoint_refresh.py) and [cre_checkpoint_series.py](cre_checkpoint_series.py) |
| Detail enrichment | [cre_guarded_tier.py](cre_guarded_tier.py) and [cre_enrich.py](cre_enrich.py) |
| Source observations and document text | [source_evidence/](source_evidence/) and [document_text/](document_text/); GetCREdata owns the listing document archive |
| Windows launcher and task tools | [windows/](windows/) |
| Tests and local setup | [tests/](tests/) and [SETUP.md](SETUP.md); collector suites require a separate standalone clone |
| Historical reviews and retired examples | [archive/](archive/); [launchd/](launchd/) is host-specific or historical, not the default Windows route |

## Root-level code by task

The root also holds operational entrypoints and their supporting modules. Start
with the entrypoint, then follow its imports and the applicable guide:

| Work | Start here |
| --- | --- |
| Refresh orchestration and recovery | [cre_checkpoint_refresh.py](cre_checkpoint_refresh.py), [cre_checkpoint_series.py](cre_checkpoint_series.py), and [cre_refresh_pipeline.py](cre_refresh_pipeline.py) |
| Admission, ingest, and readback | [cre_gate.py](cre_gate.py), [cre_ingest.py](cre_ingest.py), and [cre_validate.py](cre_validate.py) |
| Observe changes separately from ingest | [cre_monitor.py](cre_monitor.py) and [monitor subsystem guide](../../../docs/firecrawl-ops/references/cre-monitor-subsystem.md) |
| Evidence and source-specific fields | [source_evidence/](source_evidence/), [source_fields/](source_fields/), and [document_text/](document_text/) |
| Runtime handoff and maintenance | [cre_code_handoff.py](cre_code_handoff.py), [cre_runtime_maintenance.py](cre_runtime_maintenance.py), and [windows/](windows/) |
| Run measurements and capacity | [cre_performance.py](cre_performance.py), [cre_resource_usage.py](cre_resource_usage.py), [capacity_c10/](capacity_c10/), and the [telemetry guide](../../../docs/firecrawl-ops/references/cre-performance-telemetry.md) |
| Source-specific repair or backfill | Search root `cre_repair_*` and `*_backfill*` files for the source, then read the corresponding operator guide before use |

[AGENTS.md](AGENTS.md) is the canonical agent guidance; [CLAUDE.md](CLAUDE.md) points to it. [START_HERE.md](START_HERE.md) contains detailed procedures and historical context. Use the [Windows runtime and checkout guide](../../../docs/firecrawl-ops/references/development-and-runtime-checkouts.md) for development, launcher, and WSL checkout roles.

Generated run data, credentials, and private receipts are not source code. Preserve their existing locations and ownership, keep them out of Git, and use ignored `.scratch/` directories for temporary agent work. The neighboring `../cre_scrapers/` tree contains legacy research and probes rather than the active collection path.
