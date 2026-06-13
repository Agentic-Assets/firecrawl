# cre_collector/archive

Dated buildout, validation, and handoff artifacts from the 2026-06-11..13 CRE
collector build. Archived 2026-06-13 during the doc reorganization. Nothing here
is current status; live status is in `../BROKERAGE_STATUS_2026-06-12.md`,
`../START_HERE.md`, and `docs/firecrawl-ops/references/cre-intelligence-system-design.md`.

These files are kept (not deleted) because each still holds durable operational
knowledge that is not worth duplicating into the live docs but is worth keeping
findable. Index of the durable nuggets:

- `HANDOFF_LOG_2026-06-11.md` -- chronological build evidence. Durable nuggets:
  the working Cushman public API URL shape
  (`/api/properties/search?rfkId=property_search&view=pins&site_country=US&listing_type=Buy|Lease&limit=100&offset=N`)
  and the finding that some brokers' PDF URLs appear only in raw HTML (scan
  rawHtml, not just Firecrawl's extracted links); the Buildout cache/window env
  vars (`BUILDOUT_CACHE_ONLY=1`, `BUILDOUT_ASSEMBLE_FROM_CACHE=1`,
  `BUILDOUT_PAGE_START/END`, `BUILDOUT_PAGE_JITTER_MS=250,1000`) and the
  "0 selected pages missing = cache-fill success" signal; the note that direct
  public-HTTP sources (Marcus) keep working even when local Firecrawl/OrbStack is down.
- `LESSONS_2026-06-11.md` -- operational lessons and the per-source verification pattern.
- `VALIDATION_2026-06-12.md` -- full reconciliation. Durable nugget: the CBRE Deal
  Flow stale `dealflow:url:<sha1>` cleanup predicate (soft-delete url-hash rows
  superseded by a newer enriched project/PV row at the same source_url).
- `VALIDATION_GAP_AUDIT_2026-06-12.md` -- point-in-time gap list (counts now stale).
- `SUPABASE_EGRESS_AUDIT_2026-06-12.md` -- durable nugget: the read-only egress
  triage SQL methodology (`BEGIN READ ONLY`, `SET LOCAL statement_timeout`,
  `pg_column_size`, `pg_stat_statements` filtered to CRE, RLS/grant checks) and
  the board-query projection trick (`null::jsonb as raw_data`) plus its
  regression test in the display app.
- `SUPABASE_RECENT_UPLOAD_QA_2026-06-12.md`, `SUPABASE_SECURITY_NOTE_2026-06-12.md`
  -- the live security/access model is summarized in `../CLAUDE.md`
  ("Supabase access model"); these hold the point-in-time QA/perf figures.
- `CONTRACT_SYNC_2026-06-12.md`, `CODEX_GOAL_2026-06-11.md`, `goals/` -- completed
  goals and a point-in-time contract reconciliation.
- `2026-06-11-...-continued-from-a-previous-c.txt` -- raw terminal session replay;
  signal content is captured in the structured docs above.
