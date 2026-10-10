/**
 * Chromium launch and the shared browser-context factory. Every context dials
 * the loopback SSRF proxy and carries the per-request route guard, so callers
 * (/scrape, /browser-batch-fetch, /health and the C10 listener) never build a
 * context by hand.
 */
import { chromium as stealthChromium } from "playwright-extra";
import {
  Browser,
  BrowserContext,
  BrowserContextOptions,
  Route,
  Request as PlaywrightRequest,
} from "playwright";
import StealthPlugin from "puppeteer-extra-plugin-stealth";
import UserAgent from "user-agents";
import {
  HardTimeoutError,
  closeBrowserResources,
  withHardTimeout,
} from "./browser_resources";
import { BrowserResourceLeakError } from "./permit_lease";
import { ScrapeDeadlineError } from "./scrape_lifecycle";
import { TargetDnsUnavailableError } from "./target_dns";
import { type AssertSafeTargetUrl, InsecureConnectionError } from "./target_guard";

// Register stealth plugin before any launch call.
stealthChromium.use(StealthPlugin());

const AD_SERVING_DOMAINS = [
  "doubleclick.net",
  "adservice.google.com",
  "googlesyndication.com",
  "googletagservices.com",
  "googletagmanager.com",
  "google-analytics.com",
  "adsystem.com",
  "adservice.com",
  "adnxs.com",
  "ads-twitter.com",
  "facebook.net",
  "fbcdn.net",
  "amazon-adsystem.com",
];

// Belt-and-suspenders JS patches injected into every page context before
// any site script runs. These cover vectors the stealth plugin may miss.
const STEALTH_INIT_SCRIPT = `
  (() => {
    // webdriver flag
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Chrome object expected by CF bot checks
    if (!window.chrome) {
      Object.defineProperty(window, 'chrome', {
        writable: true, enumerable: true, configurable: false,
        value: {
          app: { isInstalled: false, InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }, RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' } },
          runtime: {},
          loadTimes: () => {},
          csi: () => {},
        }
      });
    }

    // Realistic plugin list
    const pluginData = [
      { name: 'Chrome PDF Plugin',  description: 'Portable Document Format', filename: 'internal-pdf-viewer', mimeTypes: [{ type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format' }] },
      { name: 'Chrome PDF Viewer',  description: '',                          filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', mimeTypes: [{ type: 'application/pdf', suffixes: 'pdf', description: '' }] },
      { name: 'Native Client',      description: '',                          filename: 'internal-nacl-plugin',  mimeTypes: [{ type: 'application/x-nacl', suffixes: '', description: 'Native Client Executable' }, { type: 'application/x-pnacl', suffixes: '', description: 'Portable Native Client Executable' }] },
    ];
    const fakePlugins = pluginData.map(p => {
      const mimes = p.mimeTypes.map(m => ({ type: m.type, suffixes: m.suffixes, description: m.description, enabledPlugin: null }));
      return { name: p.name, description: p.description, filename: p.filename, length: mimes.length, item: (i) => mimes[i], namedItem: (n) => mimes.find(m => m.type === n) || null, [Symbol.iterator]: function*() { yield* mimes; } };
    });
    Object.defineProperty(navigator, 'plugins', { get: () => Object.assign(fakePlugins, { item: (i) => fakePlugins[i], namedItem: (n) => fakePlugins.find(p => p.name === n) || null, refresh: () => {}, length: fakePlugins.length, [Symbol.iterator]: function*() { yield* fakePlugins; } }) });

    // Languages
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

    // Hardware concurrency and device memory (match a standard laptop)
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    Object.defineProperty(navigator, 'deviceMemory',        { get: () => 8 });

    // Permissions — notifications must return the real permission state
    const _origPermQuery = window.navigator.permissions.query.bind(navigator.permissions);
    window.navigator.permissions.query = (params) =>
      (params.name === 'notifications')
        ? Promise.resolve({ state: Notification.permission, onchange: null })
        : _origPermQuery(params);
  })();
`;

export type ContextSecurityState = {
  blockedNavigationRequestUrl: string | null;
  navigationDnsUnavailable: boolean;
};

export type ContextOptions = Readonly<{
  /** Lets the route guard reach private/internal hosts (local webhooks, C10 tests). */
  allowLocalTargets: boolean;
  skipTlsVerification?: boolean;
  /** Replaces the generated desktop user agent. */
  userAgent?: string;
  /** Bounds context setup; a half-configured context is closed, never returned. */
  deadlineAt?: number;
}>;

export type ContextBundle = {
  context: BrowserContext;
  securityState: ContextSecurityState;
};

export type BrowserPool = Readonly<{
  getBrowser(): Browser | undefined;
  initializeBrowser(): Promise<void>;
  createContext(options: ContextOptions): Promise<ContextBundle>;
  close(): Promise<void>;
}>;

const isMainFrameNavigation = (request: PlaywrightRequest): boolean => {
  if (!request.isNavigationRequest()) return false;
  try {
    return request.frame().parentFrame() === null;
  } catch {
    return false;
  }
};

/** Per-request SSRF guard plus ad blocking; records why a navigation failed. */
const routeGuard =
  (
    assertSafeTargetUrl: AssertSafeTargetUrl,
    securityState: ContextSecurityState,
    allowLocalTargets: boolean,
  ) =>
  async (route: Route, request: PlaywrightRequest) => {
    const requestUrlString = request.url();
    try {
      await assertSafeTargetUrl(requestUrlString, allowLocalTargets);
    } catch (error) {
      if (error instanceof TargetDnsUnavailableError) {
        // Fail closed: an unclassifiable host is never fetched. Only a
        // main-frame navigation makes the whole scrape retryable.
        if (isMainFrameNavigation(request)) {
          securityState.navigationDnsUnavailable = true;
        }
        console.warn(`Blocked request (DNS unavailable): ${requestUrlString}`);
        return route.abort("namenotresolved");
      }
      if (error instanceof InsecureConnectionError) {
        if (request.isNavigationRequest()) {
          securityState.blockedNavigationRequestUrl = requestUrlString;
        }
        console.warn(`Blocked request: ${requestUrlString}`);
        return route.abort("blockedbyclient");
      }
      throw error;
    }

    const hostname = new URL(requestUrlString).hostname.toLowerCase();

    if (AD_SERVING_DOMAINS.some((domain) => hostname.includes(domain))) {
      console.log(hostname);
      return route.abort();
    }
    return route.continue();
  };

/** Runs context setup, bounded by the scrape deadline when one is given. */
const runWithinDeadline = async (
  setup: () => Promise<void>,
  deadlineAt: number | undefined,
): Promise<void> => {
  if (deadlineAt === undefined) return setup();
  const remainingMs = deadlineAt - Date.now();
  if (remainingMs <= 0) throw new ScrapeDeadlineError("work");
  try {
    await withHardTimeout(
      setup(),
      remainingMs,
      "Browser scrape context setup exceeded its deadline",
    );
  } catch (error) {
    if (error instanceof HardTimeoutError) {
      throw new ScrapeDeadlineError("work");
    }
    throw error;
  }
};

/** Owns the one Chromium instance; launching is lazy until initializeBrowser(). */
export function createBrowserPool(settings: {
  blockMedia: boolean;
  ssrfProxyPort: number;
  /** The one SSRF check; the app and C10 routes receive the same function. */
  assertSafeTargetUrl: AssertSafeTargetUrl;
}): BrowserPool {
  let browser: Browser | undefined;

  const initializeBrowser = async () => {
    browser = await (stealthChromium.launch({
      headless: true,
      args: [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-zygote",
        "--disable-gpu",
        // Hide automation indicators
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
        "--disable-site-isolation-trials",
        // More realistic fingerprint
        "--enable-features=NetworkService,NetworkServiceLogging",
        "--lang=en-US,en",
      ],
    }) as unknown as Promise<Browser>);
  };

  const createContext = async ({
    allowLocalTargets,
    skipTlsVerification = false,
    userAgent,
    deadlineAt,
  }: ContextOptions): Promise<ContextBundle> => {
    const securityState: ContextSecurityState = {
      blockedNavigationRequestUrl: null,
      navigationDnsUnavailable: false,
    };
    const contextOptions: BrowserContextOptions = {
      userAgent:
        userAgent || new UserAgent({ deviceCategory: "desktop" }).toString(),
      viewport: { width: 1280, height: 800 },
      ignoreHTTPSErrors: skipTlsVerification,
      serviceWorkers: "block",
      locale: "en-US",
      timezoneId: "America/New_York",
      proxy: { server: `http://127.0.0.1:${settings.ssrfProxyPort}` },
    };

    const newContext = await browser!.newContext(contextOptions);

    const setup = async () => {
      // Inject stealth patches before any page script runs.
      await newContext.addInitScript(STEALTH_INIT_SCRIPT);

      if (settings.blockMedia) {
        await newContext.route(
          "**/*.{png,jpg,jpeg,gif,svg,mp3,mp4,avi,flac,ogg,wav,webm}",
          async (route: Route) => {
            await route.abort();
          },
        );
      }

      // Intercept all requests to avoid loading ads
      await newContext.route(
        "**/*",
        routeGuard(
          settings.assertSafeTargetUrl,
          securityState,
          allowLocalTargets,
        ),
      );
    };

    try {
      await runWithinDeadline(setup, deadlineAt);
    } catch (error) {
      // Never hand back (or silently drop) a half-configured context, which
      // could lack the per-request SSRF route guard above. The caller's lease
      // decides what an unconfirmed close means for its permit.
      const closed = await closeBrowserResources(null, () => newContext.close());
      throw closed ? error : new BrowserResourceLeakError(error);
    }

    return { context: newContext, securityState };
  };

  return Object.freeze({
    getBrowser: () => browser,
    initializeBrowser,
    createContext,
    close: async () => {
      if (browser) {
        await browser.close();
      }
    },
  });
}
