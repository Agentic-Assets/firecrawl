# Phase 1 adversarial review: C10 JLL controller bridge (PR #72)

- Branch: `feat/c10-jll-controller-bridge`. Reviewed diff: `6fb4d8b4e..fc68170b5`.
- Fix commits: `038650480` (sidecar lane), `afda66d90` (selection and offline
  recompute), `683fac06a` (controller hardening).
- Method: the orchestrator ran a primary cross-language review, and an
  independent Opus pass read only the committed code. Sonnet workers wrote
  the tests. No provider request, root provisioning, authority pin, DB, cache,
  listing or scheduler write, merge, or ready-for-review happened.

## Findings

| # | Claim or defect | Verdict | Evidence (pre-fix) | Fix |
|---|---|---|---|---|
| B1 | TS manifests pass the Python validator | Refuted. P0: every real manifest failed | TS `collection_intent.selection_digest` vs Python `_jll_intent()` exact equality (`jll_admission.ts` sealJllAdmissionManifest; `jll_admission.py:286`) | afda66d90 (key removed from TS intent); cross-language E2E test |
| B2 | Controller replies fit real bodies | Refuted. P0 | `_frame` capped at `_MAX_CHILD_FRAME_BYTES` = 64 KiB (`admission_controller.py:51-55`); replies carry body base64 twice | 683fac06a (8 MiB frame, matching the child); 1.5 MiB body E2E test |
| B3 | Bridge capabilities match the sidecar v3 contract | Refuted. P0 | Sidecar needs 7 binding digests and a 16-route enumeration card (`c10_browser_internal.ts` bindingFrom/cardFrom); bridge sent 6+card with no routes | 038650480 + 683fac06a: attested `C10_ADMISSION_LANE`; seven-digest binding (per-run session nonce, intent manifest digest, profile) |
| 1 | TS selection implements the rule | Refuted | `localeCompare` sort (ICU collation, not lexicographic); `String(id).trim()` accepts numbers or whitespace; WHATWG normalization of dot segments and port | afda66d90: strict slug grammar, string digit ids, code-unit comparator |
| 2 | Python controller selection equals TS | Refuted. Fails closed, no wrong-cohort path | `_source_members` used `isdigit` (Unicode digits), no trailing-slash/query folding, code-point sort (`admission_controller.py:58-95`) | afda66d90: one `select_jll_admission_members` shared by controller and validator; 42 shared golden vectors |
| 3 | Python independently recomputes selection before bundle | Refuted. P1 | `_validate_manifest` only checked `_selection_digest(manifest members)` (`jll_admission.py:320`) | afda66d90: recompute from sealed body bound via event plus `eventsSha256`; member stage artifacts bound to routes and ids |
| 4 | Controller refuses overrides; cards fully pinned | Partially refuted | `members` param silently ignored; headers, timeoutMs, maxBytes, bodySha256 and extra keys unchecked (`admission_controller.py:179-218`) | 683fac06a: no `members` param anywhere; exact card dict equality; manifest root, adapter, members and index checked against controller record |
| 5 | Tie, duplicate and <16 fail closed | Confirmed (both sides) | Dedupe before sort, so no ties; insufficient raises | Kept; tests on both sides |
| 6 | Controller loop is deadline-bounded | Refuted. P1 | Blocking `readline`/`write`; stderr PIPE never drained (`admission_controller.py:264-291`) | 683fac06a: selector-based non-blocking I/O with deadline; stderr DEVNULL; hung-child test |
| 7 | Recovery cannot accept stale artifacts | Partially refuted | `PrivateReceiptStore.create` uses `exist_ok=True`; nothing required a fresh root | 683fac06a: controller requires an existing, empty, owner-0700 root; recovery uses a new root. Index-bound rehash already ignores unindexed files |
| 8 | Child cannot escape 1+16 routes | Partially refuted | Execute 18 raised IndexError; seals unbounded in count and bytes; stems and ids unvalidated | 683fac06a: explicit graph bound, seal count and byte caps, ASCII stem and request-id grammar |
| 9 | `_verify_evidence` change kept P0/P1 strict | Refuted. P3, no practical bypass (registry always sets routes) | `expected is not None` relaxation (`host_orchestration.py:617-653`) | 038650480: strict by default; explicit `admission_enumeration=True`; P0/P1 health must report no lane |
| O1 | tsx children resolve from repository root | Refuted in this checkout | `cwd=repo_root`, which has no node_modules (loopback and controller tests failed) | 038650480/683fac06a: cwd is the collector package |
| O2 | `PrivateReceiptStore` is Linux-only | Confirmed, by design | `host_store.py:210-216` | Not changed; the live admission must run on the Linux host |

Recorded verdict (space in query): a candidate such as `/listings/a?x y` is
accepted in both languages. The query is discarded at canonicalization, and
WHATWG and the grammar agree on the resulting route. The characters that
change the route (slug charset, dot segments, host, port, whitespace outside
the query, line separators in the query) are rejected on both sides.
Accepted as intended.

## Gates

Code head `683fac06a`. The only later commit is this record.

- `cd scripts/firecrawl-ops/cre_collector && python3 -m pytest tests/ -q -p no:cacheprovider`: 3348 passed, 1 skipped, 3 warnings, 0 failed (203.6s).
- `cd scripts/firecrawl-ops/cre_collector && npx tsc --noEmit`: exit 0. `npm run test:unit`: 965 tests, 964 pass, 1 skipped, 0 fail.
- `cd apps/playwright-service-ts && npx tsc --noEmit -p .`: exit 0. `npm test`: 26 pass, 0 fail.
- `uvx ruff check` / `ruff format --check` and `python3 -m py_compile` on changed Python: clean. The knip pre-commit hook ran on each commit.

## Residual

- The sidecar image must be rebuilt with the lane change before any live run.
  Live admission is still gated on review and merge.
