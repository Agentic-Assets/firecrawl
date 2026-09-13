# Forward queue after producer fingerprint source identity repair

This is a bounded follow-up list, not publication authorization.

## Required release sequence

- Review and merge Firecrawl PR #45 through the protected repository gate.
- Review GetCREdata PR #34 independently. Apply its forward migration only
  after named shared-schema DDL approval and private rollback evidence exist.
- From a clean immutable merged Firecrawl SHA, perform a new complete
  51-source observation. Do not reconstruct v2 freshness from legacy manifests,
  partial source clocks, or the current listing snapshot.
- Build and validate the exact inventory generation, publish a receipt only
  after all 51 sources pass, then run the separately approved GetCREdata refresh
  and post-publication readback.

## Separate baseline issue

- Triage
  `test_collect_source_routes_colliers_main_through_chunk_protocol`, which fails
  on `origin/main` at `3daa1746622607c3421ceb28c284bf7742efa2fe`.
  Keep that repair out of PR #45 unless review proves a direct identity-contract
  dependency.
