# CRE throughput and failure experiments, 2026-09-09

Work tracked in [AGENTIC-1229](https://linear.app/agenticassets/issue/AGENTIC-1229). Starting collector SHA: `0ec4c3ea0edf0738b7719196b66a30e9cb61e87f`. Changes are isolated on `codex/cre-scrape-throughput`; the active full-registry generation continues on its original immutable checkout. All experiments below perform no database writes.

## Results observed

| Transport and configuration | Valid / attempted | Valid pages/minute | Interpretation |
| --- | --- | --- | --- |
| Existing browser, one requesting worker, two-CPU isolated sidecar | 24/24 | 20.66 | Same-URL sequential baseline |
| Existing browser, two pages, two CPUs, 384 PID limit | 24/24 | 37.41 | Short sample only |
| Existing browser, four pages, four CPUs, 512 PID limit, unpaced | 21/24 | 59.28 | Three source-side Akamai 403 pages; reject unpaced profile |
| Existing browser, four pages, four CPUs, client starts 500 ms apart | 24/24 | 63.66 | Short sample passed; insufficient sustained proof |
| Same existing browser, expanded sample, 500 ms starts | 103/120 | 33.40 | Four client timeouts followed by thirteen empty 403 results; reject sustained profile |
| Patched browser, four pages, four CPUs, server starts 500 ms apart | 120/120 | 87.80 | No failures |
| Same patched browser, no restart, expanded second soak | 240/240 | 89.63 | 360 successful requests across the two soaks |
| Native HTTP, one worker, preliminary unpinned diagnostic | 24/24 | 88.68 | Discovery evidence, not the production implementation |
| Native HTTP, four/eight workers, preliminary unpinned diagnostic | 24/24 each | 313.04 / 423.65 | Identity and structured payload matched; not sufficient by itself for production |
| Actual pinned JLL helper, four workers, measured minimum start gap 150 ms | 120/120 | 274.79 | No errors; 26.202 s, median 801 ms, p95 1109 ms |

Do not extrapolate these source/page measurements to full-registry end-to-end completion time. Browser results omit API conversion and ingestion. The expanded samples include different URLs from the 24-page baseline, and the host remains shared. A short passing run does not establish failure-free long-run behavior.

An initial expanded-sample command selected only 24 URLs from the legacy cache despite requesting 120. That artifact is excluded as an expanded test. The benchmark now uses the active generation's cache only for URL/expected-ID selection and fails if the actual sample size differs from the requested size. A direct pacing trial with a measured 138 ms gap was retained separately and rerun; the 274.79 result above measured at least 150 ms.

## Content fidelity

Twenty-four paired fresh direct/browser HTML captures were processed through the real JLL enrichment function. Provider IDs, canonical URLs, native property and broker payloads, contacts, brochures, documents, and media matched. Differences in photo/link sets were limited to browser cookie-consent chrome: one OneTrust logo and two consent/policy links. Native property assets were unchanged in this sample.

The paired audit applies the same Markdown converter to both HTML captures; it does not prove byte equality with Firecrawl-generated Markdown. The direct path explicitly preserves existing database Markdown and inserts newly derived Markdown only when absent. Direct transport is default-off and requires `JLL_DETAIL_TRANSPORT=direct`. The running immutable generation has not adopted this collector change.

## Failure mechanism and repairs

The earlier two-page production attempt contained 80 final empty-document log occurrences: 60 failed request messages plus 20 terminal-row duplicates, affecting 24 URLs. The previously quoted 68 was an intermediate snapshot, not a distinct-listing count. Four 125-second SDK timeouts preceded the cascade. Rollback also recreated containers, so it did not isolate page count as the initiating cause.

During this work the one-page production attempt also developed the empty-document cascade. The owned series was checkpointed with SIGTERM before recovery. This refutes the claim that the failure is exclusive to multiple browser pages. The tested browser profile is a recovery candidate, not a guarantee against every provider or network failure.

Regular browser requests previously waited indefinitely for a page permit and did not place a hard bound around context/page creation and body reads. The patch uses one absolute deadline, removes expired semaphore waiters, cleans late allocations, and holds permits until bounded cleanup completes. Successful responses are sent before cleanup so slow closure cannot consume the caller's response budget. New global start pacing is opt-in through `SCRAPE_START_INTERVAL_MS` (0 by default; accepted range 0-5000).

DNS lookup failures were treated as verified private hosts and returned empty 403 content. The patch keeps destination validation fail closed while distinguishing resolver unavailability with an explicit 503. Private/mixed DNS answers remain blocked. This explains a possible timeout-to-empty-response path, but the original lost logs do not prove its initiating cause. In the reproduced old-image 120-page failure, container counters showed no PID-limit hits or OOM kills; source denials and local empty 403s are recorded separately.

## Verification

- Final collector typecheck and full 814-test suite passed after direct pacing was incorporated into the actual helper. The direct helper spaces socket starts by at least 150 ms, including redirects, within its 15-second total deadline.
- Sidecar TypeScript build and all 25 tests passed, including existing batch tests, deadlines, late allocations, permit accounting, delivery-before-cleanup, pacing, and DNS classification.
- Independent read-only review found and fixed response delivery delayed by cleanup; revised browser/direct review found no confirmed remaining defect.
- Patched HTTP route: invalid timeout returned 400; private target remained blocked; excessive post-load wait returned `SCRAPE_WORK_TIMEOUT`/504 in 1008 ms under load.
- No production database, schema, status, OM, or scheduler mutation was performed by the experiments.

During the second browser soak, sampled host CPU was 35.87-49.15%, below the existing 75% ceiling. The isolated browser sample used about 1.96 CPU cores, 669 MiB RAM, and 151 PIDs under four CPUs, 4 GiB, and 512 PIDs. Post-soak PID-limit and OOM event counters were zero. These are samples, not a claim of continuously measured maxima.

## Upstream assessment

Fetched upstream `4d9847872f3c8e89ff7080e48778c778c04b3fb3` (September 9). Merge-base `e72fe3acac88651c31fc2ac8398926d7fa2fcdd3`; 204 upstream commits touch 504 files (+43190/-4365). An object-only merge rehearsal identified 23 conflicts. No checkout merge or runtime upgrade was performed.

There is no new upstream Playwright lifecycle implementation. Useful separate candidates are asynchronous native PDF extraction (`e33e1f6c15c6e4fc6df67dd6711743084f9a14bd`) plus its semaphore (`92e16a2bfb1662f73d2fe4b5cf7a7970b3c9c636`), per-request fetch cookie isolation (`656bffcc2883f1af5befe38766b1ff5f0469993a`), and Redis completion-write recovery (`e4ac89b025685270754afc7c970adb36224539ad`). They need their own compatibility tests and are not represented as CRE browser speed fixes. The broad merge intersects local OCR, parsing, deployment workflows, and SDK behavior and is deferred from this running refresh.

## Local runtime recovery

At 2026-09-10 01:49 UTC the patched image `sha256:43d49d17aeec695cedd3e68fdd65b1147052073b19767410c4b90846eb663257` was verified healthy on the live local sidecar: four pages, four CPUs, 4 GiB, 512 PIDs, and 500 ms start spacing. The API and queue services were not recreated. API root and end-to-end scrape smoke passed. The disposable benchmark container was stopped and removed without volumes or listing data.

At 01:50 UTC the original immutable collector generation resumed with JLL detail concurrency four, the existing two-source request setting, and the 75% host watchdog. This is a local runtime recovery, not an upstream merge or a collector-generation upgrade. The direct JLL path remains off for this generation. At the initial readback the process was alive, enumeration advanced, and sampled host CPU was about 18%. Sustained production detail recovery remains under observation.

The ignored override is `tasks/tmp/cre-speed-2026-09-09/runtime-override.yaml`. To roll back after checkpointing the owned collector and draining its queue, run `docker compose up -d --no-deps --no-build --force-recreate playwright-service` from the original checkout, then verify health before resuming. This restores the untouched original image tag and root configuration. Do not restart the API or alter the database for this rollback.

## Evidence location

The MacBook's ignored `tasks/tmp/cre-speed-2026-09-09/` folder in `Agentic-Assets/firecrawl` contains benchmark JSON, bodyless pinned-helper metrics, paired asset audit, health receipts, and public HTML snapshots. These are local-only and unavailable to other devices/cloud agents. Do not commit source bodies or credentials. This report records the portable conclusions and their proof limits.
