# Local Firecrawl Capability Matrix

Generated: `2026-08-13 07:10:53 EDT`
Route source: `apps/api/src/routes/v2.ts`
Reference source: `docs/firecrawl-ops/references/tools-capabilities.md`
Smoke source: `tasks/tmp/20260813-final-validation/smoke-final/20260813-071038-local-api-smoke.json`

| Method | Route | Local status | In ops docs | Notes |
|---|---|---|---:|---|
| `POST` | `/v2/agent` | `needs optional service` | `yes` | Skipped by default because it may enqueue an agent job when configured. |
| `DELETE` | `/v2/agent/:jobId` | `needs optional service` | `no` | requires EXTRACT_V3_BETA_URL |
| `GET` | `/v2/agent/:jobId` | `needs optional service` | `no` | requires EXTRACT_V3_BETA_URL |
| `POST` | `/v2/batch/scrape` | `works locally` | `no` | job_id=019ffad0-bd3c-7232-bd1d-26e5c7988626 |
| `DELETE` | `/v2/batch/scrape/:jobId` | `partly covered` | `no` | base async workflow is covered, this status/error/cancel variant is not directly probed |
| `GET` | `/v2/batch/scrape/:jobId` | `partly covered` | `no` | base async workflow is covered, this status/error/cancel variant is not directly probed |
| `GET` | `/v2/batch/scrape/:jobId/errors` | `partly covered` | `no` | base async workflow is covered, this status/error/cancel variant is not directly probed |
| `GET` | `/v2/browser` | `needs optional service` | `yes` | Skipped by default because browser routes depend on optional browser-service state. |
| `POST` | `/v2/browser` | `needs optional service` | `yes` | Skipped by default because it may create a browser session when configured. |
| `DELETE` | `/v2/browser/:sessionId` | `needs optional service` | `yes` | requires browser-service configuration |
| `POST` | `/v2/browser/:sessionId/execute` | `needs optional service` | `yes` | requires browser-service configuration |
| `GET` | `/v2/browser/:sessionId/replay` | `needs optional service` | `no` | requires browser-service configuration |
| `GET` | `/v2/browser/:sessionId/replay/:pageId` | `needs optional service` | `no` | requires browser-service configuration |
| `POST` | `/v2/browser/webhook/destroyed` | `needs optional service` | `no` | requires browser-service configuration |
| `GET` | `/v2/concurrency-check` | `not tested` | `no` | diagnostic route registered but not in local smoke matrix |
| `POST` | `/v2/crawl` | `works locally` | `yes` | job_id=019ffad0-e8df-726a-9bb9-7403d9a7138d |
| `DELETE` | `/v2/crawl/:jobId` | `partly covered` | `no` | base async workflow is covered, this status/error/cancel variant is not directly probed |
| `GET` | `/v2/crawl/:jobId` | `partly covered` | `no` | base async workflow is covered, this status/error/cancel variant is not directly probed |
| `WS` | `/v2/crawl/:jobId` | `partly covered` | `no` | base async workflow is covered, this status/error/cancel variant is not directly probed |
| `GET` | `/v2/crawl/:jobId/errors` | `partly covered` | `no` | base async workflow is covered, this status/error/cancel variant is not directly probed |
| `GET` | `/v2/crawl/active` | `works locally` | `yes` | active=0 |
| `GET` | `/v2/crawl/ongoing` | `partly covered` | `no` | base async workflow is covered, this status/error/cancel variant is not directly probed |
| `POST` | `/v2/crawl/params-preview` | `needs model env` | `yes` | LLM-backed crawl option generation |
| `POST` | `/v2/extract` | `needs model env` | `yes` | deprecated v2 extract path requires schema and model provider env |
| `GET` | `/v2/extract/:jobId` | `needs model env` | `no` | deprecated v2 extract path requires schema and model provider env |
| `POST` | `/v2/feedback` | `not tested` | `no` | feedback route registered but not in local smoke matrix |
| `GET` | `/v2/interact` | `needs optional service` | `yes` | requires browser-service configuration |
| `POST` | `/v2/interact` | `needs optional service` | `yes` | requires browser-service configuration |
| `DELETE` | `/v2/interact/:sessionId` | `needs optional service` | `no` | requires browser-service configuration |
| `POST` | `/v2/interact/:sessionId/execute` | `needs optional service` | `no` | requires browser-service configuration |
| `GET` | `/v2/interact/:sessionId/replay` | `needs optional service` | `no` | requires browser-service configuration |
| `GET` | `/v2/interact/:sessionId/replay/:pageId` | `needs optional service` | `no` | requires browser-service configuration |
| `GET` | `/v2/keyless/eligibility` | `not tested` | `no` | diagnostic route registered but not in local smoke matrix |
| `POST` | `/v2/map` | `works locally` | `yes` | links=0 |
| `GET` | `/v2/monitor` | `hosted or configured only` | `yes` | monitor backend is not part of the default local ops stack |
| `POST` | `/v2/monitor` | `hosted or configured only` | `yes` | monitor backend is not part of the default local ops stack |
| `DELETE` | `/v2/monitor/:monitorId` | `hosted or configured only` | `no` | monitor backend is not part of the default local ops stack |
| `GET` | `/v2/monitor/:monitorId` | `hosted or configured only` | `no` | monitor backend is not part of the default local ops stack |
| `PATCH` | `/v2/monitor/:monitorId` | `hosted or configured only` | `no` | monitor backend is not part of the default local ops stack |
| `GET` | `/v2/monitor/:monitorId/checks` | `hosted or configured only` | `no` | monitor backend is not part of the default local ops stack |
| `GET` | `/v2/monitor/:monitorId/checks/:checkId` | `hosted or configured only` | `no` | monitor backend is not part of the default local ops stack |
| `POST` | `/v2/monitor/:monitorId/run` | `hosted or configured only` | `no` | monitor backend is not part of the default local ops stack |
| `POST` | `/v2/monitor/email/confirm` | `hosted or configured only` | `no` | monitor backend is not part of the default local ops stack |
| `POST` | `/v2/monitor/email/unsubscribe` | `hosted or configured only` | `no` | monitor backend is not part of the default local ops stack |
| `POST` | `/v2/parse` | `works locally` | `yes` | markdown_len=415 |
| `POST` | `/v2/parse/upload-url` | `not tested` | `no` | registered route is not covered by the latest local smoke matrix |
| `POST` | `/v2/scrape` | `works locally` | `yes` | markdown_len=180 |
| `GET` | `/v2/scrape/:jobId` | `partly covered` | `no` | base async workflow is covered, this status/error/cancel variant is not directly probed |
| `DELETE` | `/v2/scrape/:jobId/interact` | `needs optional service` | `no` | requires browser-service or interactive scrape support |
| `POST` | `/v2/scrape/:jobId/interact` | `needs optional service` | `no` | requires browser-service or interactive scrape support |
| `POST` | `/v2/search` | `works locally` | `yes` | results=2 |
| `POST` | `/v2/search/:jobId/feedback` | `not tested` | `no` | feedback route registered but not in local smoke matrix |
| `GET` | `/v2/slack/channels` | `not tested` | `no` | registered route is not covered by the latest local smoke matrix |
| `POST` | `/v2/slack/commands` | `not tested` | `no` | registered route is not covered by the latest local smoke matrix |
| `POST` | `/v2/slack/events` | `not tested` | `no` | registered route is not covered by the latest local smoke matrix |
| `DELETE` | `/v2/slack/installation` | `not tested` | `no` | registered route is not covered by the latest local smoke matrix |
| `GET` | `/v2/slack/oauth/callback` | `not tested` | `no` | registered route is not covered by the latest local smoke matrix |
| `POST` | `/v2/slack/oauth/start` | `not tested` | `no` | registered route is not covered by the latest local smoke matrix |
| `GET` | `/v2/slack/status` | `not tested` | `no` | registered route is not covered by the latest local smoke matrix |
| `POST` | `/v2/support/ask` | `needs optional service` | `no` | Skipped by default because it may call an external support service when configured. |
| `POST` | `/v2/support/docs-search` | `needs optional service` | `no` | requires SUPPORT_AGENT_URL |
| `GET` | `/v2/team/activity` | `not tested` | `no` | diagnostic route registered but not in local smoke matrix |
| `GET` | `/v2/team/credit-usage` | `not tested` | `no` | accounting route registered but not in local smoke matrix |
| `GET` | `/v2/team/credit-usage/historical` | `not tested` | `no` | accounting route registered but not in local smoke matrix |
| `GET` | `/v2/team/queue-status` | `works locally` | `yes` | jobsInQueue=0 |
| `GET` | `/v2/team/siem` | `not tested` | `no` | registered route is not covered by the latest local smoke matrix |
| `POST` | `/v2/team/siem/test` | `not tested` | `no` | registered route is not covered by the latest local smoke matrix |
| `GET` | `/v2/team/threat-protection` | `not tested` | `no` | registered route is not covered by the latest local smoke matrix |
| `GET` | `/v2/team/threat-protection/zscaler/categories` | `not tested` | `no` | registered route is not covered by the latest local smoke matrix |
| `POST` | `/v2/team/threat-protection/zscaler/sync` | `not tested` | `no` | registered route is not covered by the latest local smoke matrix |
| `POST` | `/v2/team/threat-protection/zscaler/test-connection` | `not tested` | `no` | registered route is not covered by the latest local smoke matrix |
| `GET` | `/v2/team/token-usage` | `not tested` | `no` | accounting route registered but not in local smoke matrix |
| `GET` | `/v2/team/token-usage/historical` | `not tested` | `no` | accounting route registered but not in local smoke matrix |
