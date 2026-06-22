# services/monitoring/ -- website change monitoring

Self-contained subsystem. A monitor watches a set of URLs/crawls on a cron schedule and reports diffs.

## Data model (types.ts)

- `MonitorRow` -- persisted monitor (id, team_id, schedule_cron, targets, retention_days, goal, judge_enabled)
- `MonitorCheckRow` -- one check run; status: `queued | running | completed | failed | partial | skipped_overlap`
- `MonitorPageRow` -- per-URL result within a check; status: `same | new | changed | removed | error`
- `MonitorCheckPageInsert` -- write shape for page results; includes optional `judgment` field
- `MonitorTarget` -- either `{ type: "scrape", urls }` or `{ type: "crawl", url, crawlOptions }`
- `withMarkdownFormat(options)` -- utility to ensure markdown is always in scrape formats

## Schema (types.ts)

- `createMonitorSchema` / `updateMonitorSchema` -- Zod schemas; schedule accepts `{ cron }` or `{ text }` (natural language parsed by `cron.ts`); text is transformed to cron string on input
- `createWebhookSchema(["monitor.page", "monitor.check.completed"])` from `services/webhook/schema`
- `applyJudgeEnabledDefault` -- auto-enables `judgeEnabled` when a goal string is present

## Key modules

| File | Purpose |
|------|---------|
| `runner.ts` | Main check executor. `runMonitorCheck(checkId)` scrapes all target URLs, calls diff-orchestrator, emits webhooks and email summaries. |
| `store.ts` | All DB read/write: `getMonitorForUpdate`, `upsertMonitorPage`, `insertMonitorCheckPages`, `updateMonitorCheck`, `updateMonitorScheduleAfterRun`, `hashMonitorUrl`. |
| `diff.ts` | `computeDiff(prev, curr)`, `normalizeMonitorFormats`. Returns a structured diff object. |
| `diff-orchestrator.ts` | `computeAndPersistPageDiff` -- calls diff.ts and stores artifacts to GCS (`gcs-monitoring.ts`). |
| `judgeChange.ts` | LLM-based meaningful change judgment. Called per page when `judgeEnabled=true`. |
| `scheduler.ts` | Cron scheduling: picks due monitors, enqueues check jobs via `queue.ts`. |
| `cron.ts` | `parseMonitorScheduleText(text)` -- converts natural language schedules to cron expressions. |
| `interest.ts` | Tracks which teams have expressed interest in monitoring (for notification targeting). |
| `stale.ts` | `isMonitorCheckStale` / `MONITOR_CHECK_STALE_TIMEOUT_MS` -- guards against zombie checks. |
| `email_recipients.ts` | Resolves email recipients for a monitor. `email_recipients_sync.ts` handles DB sync. |
| `results.ts` | Aggregates page results into check summaries. |

## Conventions

- Locking: `getMonitorForUpdate` uses a DB row lock; always call inside a transaction
- GCS keys for diffs: `monitor-diffs/<monitor_id>/<check_id>/<url_hash>.[json|txt]`
- Redis eviction connection (`redisEvictConnection`) used for notification dedup claim TTL (7 days)
- Monitoring scrapes always inject markdown format via `withMarkdownFormat`
