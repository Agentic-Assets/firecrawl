# C10 JLL admission lane progress and forward queue

## Verified repository state

- PR #67 is merged production-host hardening. It is not evidence of a live
  provider run.
- PR #70 is merged offline reviewed-admission-chain support. Its 20-source
  compatibility panel remains non-admitting: blocked or unavailable rows are
  never executable C10 evidence.
- PR #71 is merged JLL-only admission-lane support. It introduces a separate
  JLL authority, cohort, plan, source-card intent and sealed receipt artifact
  index. It does not change the 20-source authority.

## What this branch proved locally

The fixed JLL receipt producer accepts only the reviewed sale/office/page-1
GraphQL request plus sixteen canonical JLL members through a controller-owned,
source-bound one-shot transport. Its manifest records every sealed artifact.
Before bundle or authority rendering, Python reopens every indexed owner-0600
artifact from a retained owner-0700 directory descriptor and checks byte count
and SHA-256. The collection intent commits the source POST-body hash and exact
member-route order, and the rendered plan binds that intent and receipt binding.

## Discovered controller bridge and unfinished external gates

The post-merge preflight found that the offline lane had no production-owned
TypeScript execution bridge. This branch supplies that missing boundary as a
typed framed child: the controller alone issues each signed C10 capability,
executes the loopback request, verifies evidence, and seals descriptor-relative
artifacts. The child can only request the fixed enumeration followed by the
sealed sixteen routes. It cannot select a root, call a provider, or create a
receipt outside the active production-controller authority.
The source, not the controller, deterministically selects the 16 members from
the signed enumeration using the versioned canonical-URL ordering rule; Python
recomputes and verifies the emitted selection digest before it can build a bundle.

No provider request, authority pin, P0/P1 arm, database/cache/listing write,
or live C10 result exists. After this bridge is reviewed and merged, the
required operator sequence is: provision fresh private roots; invoke
`production.execute_jll_admission_collection` through the reviewed operator
surface (not the refusing offline CLI);
review the rendered separate authority proposal; make a reviewed repository
pin; then run the existing production-controller dry-run before any bounded P0
arm. Preserve the receipt manifest and final outcome as operator evidence. Do
not treat this document or a passing mock test as live provider proof.

## Finalization: 2026-09-16

The open items from the earlier stop handoff are closed on
[PR #72](https://github.com/Agentic-Assets/firecrawl/pull/72). Review records:
`docs/logs/subagents/2026-09-16/c10-jll-controller-bridge/phase1-adversarial-review.md`
and `phase2-live-readiness-fixes.md` in the same folder.

### Phase 1: cross-language adversarial review (`038650480`, `afda66d90`, `683fac06a`)

Before this pass the bridge could not have completed a live collection, even
though its earlier tests passed:

- Python rejected every manifest the TS child wrote (extra `selection_digest`
  in `collection_intent`).
- Controller replies were capped at 64 KiB, below real base64 response sizes;
  the cap is now 8 MiB, matching the child.
- Bridge cards did not satisfy the sidecar v3 contract. The sidecar now has a
  named JLL-only admission lane reported in signed health; P0/P1 stay strict
  and require no lane.

Also fixed: locale-sensitive TS sort replaced with code-unit order and a strict
route grammar; one shared Python selector with 42 golden vectors checked in
both languages; Python recomputes the selection from the sealed enumeration
body before any bundle; the ignored `members` override removed and cards
pinned exactly; deadline-bounded non-blocking child I/O; fresh empty 0700
receipt root required; bounded seal count, bytes, stems and request ids.

### Independent review and phase 2 (`39962fad1`, `a1f53bf25`, `44584a307`, `cfb6ccb5c`)

A separate reviewer approved the offline code but confirmed live-run defects,
now fixed with tests:

- Teardown ran under the expired run deadline and leaked the sidecar. It now
  has its own bounded budget with retries; unproven teardown keeps the lock and
  armed marker and writes a quarantine record naming the compose project.
- Health was a single request after `compose up`. The shared health check
  (P0/P1 too) now retries only connection refused/reset, up to 60 s; every
  HTTP response still goes through signed-health verification.
- Budgets split: startup and health 180 s, collection 570 s (17 x 30 s + 60 s),
  teardown 60 s; worst case 815 s including child reaping.
- The offline builder accepted roots the controller rejected. The controller
  now seals a completion attestation only after verification, and the builder
  and renderers require it and refuse quarantined roots.
- Adapter digest checked before lock or sidecar work; killed child reaped;
  admission holds the shared C10 lock; GraphQL `errors: []` means no errors in
  TS sidecar, Python evidence check and selection (11 shared vectors).

### Gates on exact head `cfb6ccb5c`

| Gate | Result |
| --- | --- |
| Collector `python3 -m pytest tests/ -q -p no:cacheprovider` | 3447 passed, 1 skipped |
| Collector `npx tsc --noEmit` | pass |
| Collector `npm run test:unit` | 976 passed, 1 skipped |
| `apps/playwright-service-ts` `npx tsc --noEmit -p .` | pass |
| `apps/playwright-service-ts` `npm test` | 60 passed |
| `uvx ruff check`, `ruff format --check`, `py_compile` (changed Python) | clean |
| knip pre-commit hook | ran on every commit, never bypassed |

A deslop gate ran after each phase; the second pass also refuted P0/P1
regressions, lock release with a live container, child forgery of the
completion attestation, and unverified readiness acceptance.

### Operator-visible changes

- C10 runs (P0/P1 and admission) never build or pull the sidecar image. Build
  it from the reviewed checkout first:
  `docker compose -f docker-compose.yaml -f docker-compose.c10.yaml build playwright-service-c10`.
- The private receipt store is Linux-only; the live admission must run on the
  Linux production host.
- Quarantine recovery is by compose project label:
  `docker ps --all --filter label=com.docker.compose.project=<project>`.

### Known live-run risks (unchanged by design)

- A response body starting with a UTF-8 BOM is rejected.
- The existing challenge regex matches "captcha" in member HTML and would fail
  closed on a page embedding reCAPTCHA.
- The 815 s bound and readiness behavior are proven against fakes and the local
  loopback listener, not a real Docker sidecar or JLL.
- The offline checker validates `session_sha256`/`run_sha256` shape only; a
  same-uid process could write matching files, as it could the manifest itself.

### Still not done (separately gated)

No provider request, root provisioning, authority pin, P0/P1 arm, database,
cache, listing or scheduler write, or merge occurred. Remaining sequence after
merge approval: rebuild the sidecar image from the merged checkout; provision
a fresh 0700 root on the Linux host; run the reviewed bounded JLL admission
action once; build the bundle and render (not install) authority; open a
distinct reviewed pin PR; then run the production-controller dry-run before any
bounded P0 arm. Do not treat this document or passing tests as live provider
proof.
