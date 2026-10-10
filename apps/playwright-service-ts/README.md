# Playwright Scrape API

This is a simple web scraping service built with Express and Playwright.

## Features

- Scrapes HTML content from specified URLs.
- Blocks requests to known ad-serving domains.
- Blocks media files to reduce bandwidth usage.
- Uses random user-agent strings to avoid detection.
- Strategy to ensure the page is fully rendered.

## Install
```bash
npm install
npx playwright install
```

## RUN
```bash
npm run build
npm start
```
OR
```bash
npm run dev
```

## USE

```bash
curl -X POST http://localhost:3000/scrape \
-H "Content-Type: application/json" \
-d '{
  "url": "https://example.com",
  "wait_after_load": 1000,
  "timeout": 15000,
  "headers": {
    "Custom-Header": "value"
  },
  "check_selector": "#content"
}'
```

## USING WITH FIRECRAWL

Add `PLAYWRIGHT_MICROSERVICE_URL=http://localhost:3003/scrape` to `/apps/api/.env` to configure the API to use this Playwright microservice for scraping operations.

## `/scrape` deadline and failure codes

An explicit `timeout` is the whole request budget: target validation, waiting
for a page permit (`MAX_CONCURRENT_PAGES`), optional pacing, browser context
setup, navigation, `wait_after_load`, and body reads. When `timeout` is
omitted, the budget is 15000 ms plus `wait_after_load`. Firecrawl's API
sends its remaining scrape time as `timeout` only when the scrape has its
own timeout; v2 `/scrape` and crawl requests without one omit it, so the
default applies and includes the permit queue. Under saturation those
requests get `503` `SCRAPE_ADMISSION_TIMEOUT` after about 15 s of queueing,
and the API falls through to its next engine. `timeout` and
`wait_after_load` are each capped at 86400000 ms (24 hours), well below the
2^31-1 ms Node timer limit; larger, non-positive, or non-numeric values
return `400`. Playwright's own navigation and selector timeouts get a 250 ms
grace past the deadline, so an expiry during work is always reported as
`504` `SCRAPE_WORK_TIMEOUT`, not as a generic `500`.

| Response | Meaning |
| --- | --- |
| `200` with `pageStatusCode: 403` | Target resolves to a private/internal address, including bracketed IPv6 literals such as `[::1]` (blocked, not retryable) |
| `503` `TARGET_DNS_UNAVAILABLE` | Target DNS could not be resolved, so nothing was fetched (retryable) |
| `503` `SCRAPE_ADMISSION_TIMEOUT` | Deadline passed during validation, queueing, or pacing; no browser context was allocated |
| `504` `SCRAPE_WORK_TIMEOUT` | Deadline passed during context setup, navigation, or body reads |
| `503` `SCRAPE_RESOURCE_LEAK` | A partial browser context could not be confirmed closed; its permit stays quarantined |

DNS failures stay fail closed: a host that cannot be classified is never
fetched by the route guard or the SSRF proxy. A permit is released only after
its browser resources are confirmed closed, the same policy as
`/browser-batch-fetch`. A context that finishes allocating after the deadline
keeps the permit until it is closed. `/browser-batch-fetch` also returns `503`
`TARGET_DNS_UNAVAILABLE` (instead of `400`) when its bootstrap host cannot be
resolved.

`TARGET_DNS_UNAVAILABLE` covers only the requested URL. Playwright's route
guard sees the first URL of a redirect chain, so a DNS failure on a later
redirect hop is refused by the SSRF proxy (`502`) instead. That scrape fails
as an ordinary navigation error, not as `TARGET_DNS_UNAVAILABLE`. The hop is
still never fetched.

## Optional scrape pacing

Set `SCRAPE_START_INTERVAL_MS` on this service to an integer from `0` to `5000`
to space regular `/scrape` starts globally. The default `0` leaves starts
unpaced. Pacing occurs after acquiring a browser-page permit and before
allocating a context. Its waiting time consumes the request deadline; a
request that expires while paced returns `503` `SCRAPE_ADMISSION_TIMEOUT` and
releases its permit without starting a browser context. Invalid values stop
the service at startup. The batch endpoint and the C10 listener are
unaffected. Choose any nonzero interval from measured provider behavior and
resource limits before deploying it.
