# Playwright Stealth — Cloudflare Bypass

Verified working 2026-06-11 against CBRE (`www.cbre.com`) using Cloudflare Managed
Challenge. Three code changes are required. This document explains what was changed,
why each change was necessary, and how to replicate if the stack is re-cloned.

## Background

Cloudflare Managed Challenge (interactive type) validates browser fingerprints with
JavaScript challenges. Standard headless Chromium fails because:

1. `navigator.webdriver` is `true`
2. `window.chrome` is absent
3. `navigator.plugins` is empty
4. `navigator.languages` / `hardwareConcurrency` / `deviceMemory` look like automation
5. Playwright's `--disable-accelerated-2d-canvas` flag is itself a bot signal

Additionally, Firecrawl's engine selection system gates each engine behind feature flags.
When `proxy: "stealth"` is used (or Firecrawl auto-detects a 403 from Cloudflare), it
adds a `stealthProxy` feature flag and only considers engines where
`features.stealthProxy === true`. Without Fix 3 below, playwright was silently filtered
out of the candidate set even after Fixes 1 and 2 made it capable of stealth operation.

## Fix 1: Add stealth dependencies to playwright-service

**File:** `apps/playwright-service-ts/package.json`

Add to `"dependencies"`:

```json
"playwright-extra": "^4.3.6",
"puppeteer-extra-plugin-stealth": "^2.11.2"
```

`playwright-extra` is a thin wrapper that lets you attach plugins to playwright.
`puppeteer-extra-plugin-stealth` patches the most common automation detection vectors
at the V8 level before any page JavaScript runs.

The Dockerfile for this service uses plain `npm install`, so adding these to
`package.json` is sufficient — no lockfile update, no Dockerfile change. Rebuild
the container after editing `package.json`.

## Fix 2: Rewrite playwright-service api.ts to use stealth engine

**File:** `apps/playwright-service-ts/api.ts`

### 2a — Import playwright-extra instead of playwright

```typescript
// Before
import { chromium } from 'playwright';

// After
import { chromium as stealthChromium } from 'playwright-extra';
import { Browser, BrowserContext, Route, Request as PlaywrightRequest, Page } from 'playwright';
import StealthPlugin from 'puppeteer-extra-plugin-stealth';

stealthChromium.use(StealthPlugin());
```

The plugin must be registered before any `launch()` call.

### 2b — Fix browser launch args

Remove `--disable-accelerated-2d-canvas` (a known bot signal). Add:

```typescript
args: [
  '--no-sandbox',
  '--disable-setuid-sandbox',
  '--disable-dev-shm-usage',
  '--no-first-run',
  '--no-zygote',
  '--disable-gpu',
  '--disable-blink-features=AutomationControlled',
  '--disable-features=IsolateOrigins,site-per-process',
  '--disable-site-isolation-trials',
  '--enable-features=NetworkService,NetworkServiceLogging',
  '--lang=en-US,en',
]
```

### 2c — Inject `STEALTH_INIT_SCRIPT` into every browser context

The stealth plugin patches most vectors, but a belt-and-suspenders JS init script
covers additional checks that Cloudflare specifically looks for. Inject it via
`context.addInitScript()` so it runs before any site JS:

```typescript
const STEALTH_INIT_SCRIPT = `
  (() => {
    // 1. webdriver flag
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // 2. Chrome runtime object (CF checks window.chrome.runtime)
    if (!window.chrome) {
      Object.defineProperty(window, 'chrome', {
        writable: true, enumerable: true, configurable: false,
        value: {
          app: { isInstalled: false, ... },
          runtime: {},
          loadTimes: () => {},
          csi: () => {},
        }
      });
    }

    // 3. Realistic 3-plugin array
    // Chrome PDF Plugin, Chrome PDF Viewer, Native Client
    // (details omitted — see full api.ts for the complete definition)

    // 4. Navigator properties
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    Object.defineProperty(navigator, 'deviceMemory',        { get: () => 8 });

    // 5. Permissions.query — notifications must return real state
    const _origPermQuery = window.navigator.permissions.query.bind(navigator.permissions);
    window.navigator.permissions.query = (params) =>
      (params.name === 'notifications')
        ? Promise.resolve({ state: Notification.permission, onchange: null })
        : _origPermQuery(params);
  })();
`;
```

### 2d — Set locale and timezone on the browser context

```typescript
const contextOptions = {
  userAgent,
  viewport: { width: 1280, height: 800 },
  ignoreHTTPSErrors: skipTlsVerification,
  serviceWorkers: 'block',
  locale: 'en-US',           // added
  timezoneId: 'America/New_York',  // added
};
```

### 2e — Use a desktop user-agent

```typescript
const userAgent = new UserAgent({ deviceCategory: 'desktop' }).toString();
```

The `user-agents` package (already in the original `package.json`) returns a
realistic rotating desktop Chrome UA string. Without `deviceCategory: 'desktop'`
it may return a mobile UA, which Cloudflare can use to detect inconsistency with
the desktop `window.screen` dimensions.

## Fix 3: Enable stealthProxy feature flag on the playwright engine

**File:** `apps/api/src/scraper/scrapeURL/engines/index.ts`

This is the most important change. Find the `playwright` engine entry (around line 346)
and change `stealthProxy` from `false` to `true`:

```typescript
playwright: {
  features: {
    actions: false,
    waitFor: true,
    screenshot: false,
    "screenshot@fullScreen": false,
    pdf: false,
    document: false,
    audio: false,
    video: false,
    atsv: false,
    location: false,
    mobile: false,
    skipTlsVerification: true,
    useFastMode: false,
    stealthProxy: true,    // was false
    branding: false,
    disableAdblock: false,
  },
  quality: 20,
},
```

**Why this matters:** When `proxy: "stealth"` is passed on a scrape request, or when
Firecrawl detects a 403/401/429 from an anti-bot system, the engine selector adds
`stealthProxy` to the required feature flags set. Any engine whose feature map has
`stealthProxy: false` is excluded from the candidate list. Without this fix, the
selector runs, excludes playwright, finds no viable engine, and exhausts retries with
`SCRAPE_RETRY_LIMIT / document_antibot`. With this fix, playwright is included and
selected (quality score 20), stealth rendering proceeds, and Cloudflare is bypassed.

## Applying the changes

After editing all three files:

```bash
# Rebuild only the affected services
docker compose build playwright-service api
docker compose up -d playwright-service api

# Verify the stack
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

The playwright-service rebuild takes the longest (~2-3 minutes) because it installs
`playwright-extra` and the stealth plugin via `npm install`.

## Testing

```bash
# Single CBRE property — should complete in ~10-20 seconds
curl -sS -X POST http://localhost:3002/v2/scrape \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.cbre.com/properties/properties-for-lease/commercial-space/details/US-SMPL-6130/slug",
    "formats": ["markdown"],
    "proxy": "stealth",
    "waitFor": 6000,
    "timeout": 60000
  }' | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('success'), len(d.get('data',{}).get('markdown','')))"
```

Expected output: `True <number > 500>`. If you see `False` or markdown length near 0,
check the API and playwright-service logs for `document_antibot` or stealth plugin
registration errors.

## What does NOT work without these changes

| Approach | Result |
|----------|--------|
| `proxy: "stealth"` without Fix 3 | `SCRAPE_RETRY_LIMIT / document_antibot` — playwright filtered from engine selection |
| `proxy: "basic"` | 403 from Cloudflare — no JS execution |
| `proxy: "auto"` without Fire Engine | Same as above; auto upgrades to stealth after 403 but then hits the engine filter |
| Plain playwright without Fix 2 | Detectable headless; Cloudflare challenge never resolves |

## Notes on upstream divergence

Fixes 1 and 2 modify `apps/playwright-service-ts/` — this is a service that upstream
also maintains. On the next upstream sync, check whether `api.ts` has been updated and
re-apply the stealth changes if the file is overwritten. Fix 3 (`engines/index.ts`) is
in `apps/api/` — same caution applies.

A future upstream PR to make playwright stealth-capable would make these patches
unnecessary, but as of 2026-06-11 the upstream playwright service uses plain Chromium
with no stealth plugin.
