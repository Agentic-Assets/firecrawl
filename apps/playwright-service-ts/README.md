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

## Optional scrape pacing

Set `SCRAPE_START_INTERVAL_MS` on this service to an integer from `0` to `5000`
to space regular `/scrape` starts globally. The default `0` leaves starts
unpaced. Pacing occurs after acquiring a browser-page permit and before
allocating a context. Its waiting time consumes the existing request timeout;
an expired request releases its permit without starting a browser context.
The batch endpoint is unaffected. Choose any nonzero interval from measured
provider behavior and resource limits before deploying it.
