/**
 * POST /scrape: input validation, the lifecycle wiring, page scraping, caller
 * header handling, and the failure table that maps errors to HTTP responses.
 */
import type { Request, Response } from "express";
import type { BrowserContext, Page } from "playwright";
import type { BrowserPool, ContextSecurityState } from "./browser_context";
import type { Semaphore } from "./browser_resources";
import { getError } from "./helpers/get_error";
import {
  parseScrapeTiming,
  PLAYWRIGHT_DEADLINE_GRACE_MS,
  runScrapeLifecycle,
  ScrapeClientGoneError,
  ScrapeDeadlineError,
  ScrapeResourceLeakError,
} from "./scrape_lifecycle";
import { TargetDnsUnavailableError } from "./target_dns";
import { type AssertSafeTargetUrl, InsecureConnectionError } from "./target_guard";

interface UrlModel {
  url: string;
  wait_after_load?: number;
  timeout?: number;
  headers?: { [key: string]: string };
  check_selector?: string;
  skip_tls_verification?: boolean;
}

export type ScrapeRouteDeps = Readonly<{
  browsers: Pick<BrowserPool, "getBrowser" | "initializeBrowser" | "createContext">;
  pageSemaphore: Semaphore;
  /** Present only when SCRAPE_START_INTERVAL_MS > 0. */
  paceStart?: (remainingMs: () => number) => Promise<void>;
  allowLocalTargets: boolean;
  proxyConfigured: boolean;
  assertSafeTargetUrl: AssertSafeTargetUrl;
}>;

type ScrapeFailureCode =
  | TargetDnsUnavailableError["code"]
  | ScrapeDeadlineError["code"]
  | ScrapeResourceLeakError["code"];

/**
 * The README's /scrape failure table, as code. A blocked private target is
 * the one legacy row outside it: HTTP 200 carrying pageStatusCode 403.
 */
const SCRAPE_FAILURE_STATUS: Record<ScrapeFailureCode, number> = {
  // Retryable: the target could not be classified, so nothing was fetched.
  TARGET_DNS_UNAVAILABLE: 503,
  SCRAPE_ADMISSION_TIMEOUT: 503,
  SCRAPE_WORK_TIMEOUT: 504,
  SCRAPE_RESOURCE_LEAK: 503,
};

type ScrapeFailureResponse = {
  status: number;
  body: Record<string, unknown>;
  /** console.error prefix; unset for failures the route does not log. */
  logAs?: string;
};

/** Maps a lifecycle failure that can still be answered to its HTTP response. */
function scrapeFailureResponse(error: unknown): ScrapeFailureResponse {
  if (error instanceof InsecureConnectionError) {
    // Legacy contract: a blocked private target is not an HTTP failure.
    return {
      status: 200,
      body: { content: "", pageStatusCode: 403, pageError: error.message },
    };
  }
  if (
    error instanceof TargetDnsUnavailableError ||
    error instanceof ScrapeDeadlineError ||
    error instanceof ScrapeResourceLeakError
  ) {
    return {
      status: SCRAPE_FAILURE_STATUS[error.code],
      body: { error: error.message, code: error.code },
      logAs:
        error instanceof ScrapeResourceLeakError
          ? "Scrape resource leak:"
          : undefined,
    };
  }
  return {
    status: 500,
    body: { error: "An error occurred while fetching the page." },
    logAs: "Scrape error:",
  };
}

const isValidUrl = (urlString: string): boolean => {
  try {
    new URL(urlString);
    return true;
  } catch (_) {
    return false;
  }
};

const scrapePage = async (
  page: Page,
  url: string,
  waitUntil: "load" | "networkidle",
  waitAfterLoad: number,
  timeout: number,
  checkSelector: string | undefined,
  securityState: ContextSecurityState,
) => {
  console.log(
    `Navigating to ${url} with waitUntil: ${waitUntil} and timeout: ${timeout}ms`,
  );
  let response;
  try {
    response = await page.goto(url, { waitUntil, timeout });
  } catch (error) {
    // A blocked private destination wins over a DNS outage in the same chain.
    if (securityState.blockedNavigationRequestUrl) {
      throw new InsecureConnectionError(
        securityState.blockedNavigationRequestUrl,
        "navigation to private/internal resource is not allowed",
      );
    }
    if (securityState.navigationDnsUnavailable) {
      throw new TargetDnsUnavailableError();
    }
    throw error;
  }

  if (waitAfterLoad > 0) {
    await page.waitForTimeout(waitAfterLoad);
  }

  if (checkSelector) {
    try {
      await page.waitForSelector(checkSelector, { timeout });
    } catch (error) {
      throw new Error("Required selector not found");
    }
  }

  let headers = null,
    content = await page.content(),
    landedUrl = page.url();
  let ct: string | undefined = undefined;
  if (response) {
    headers = await response.allHeaders();
    ct = Object.entries(headers).find(
      ([key]) => key.toLowerCase() === "content-type",
    )?.[1];
    if (
      ct &&
      (ct.toLowerCase().includes("application/json") ||
        ct.toLowerCase().includes("text/plain"))
    ) {
      content = (await response.body()).toString("utf8"); // TODO: determine real encoding
      landedUrl = response.url();
    }
  }

  return {
    content,
    // Upstream #4862. Where the returned content came from: page.url() for
    // rendered HTML (covers HTTP 3xx and client-side redirects),
    // response.url() for raw JSON/text bodies, which come from the page.goto
    // response.
    url: landedUrl,
    status: response ? response.status() : null,
    headers,
    contentType: ct,
  };
};

// Applies caller headers to a fresh context/page before navigation.
const applyScrapeHeaders = async (
  requestContext: BrowserContext,
  page: Page,
  url: string,
  headers: { [key: string]: string },
): Promise<void> => {
  // A Cookie header passed through setExtraHTTPHeaders is sent on the first
  // request but DROPPED on any redirect hop (the browser regenerates the
  // redirected request from its cookie jar, which is empty). Authenticated
  // sites that 302 (e.g. to /signin when the session looks absent) then
  // land on the login page. Seed the cookie jar instead so Chromium re-sends
  // it on every request, including redirects — matching what a raw HTTP
  // client does.
  const cookieHeader = Object.entries(headers).find(
    ([k]) => k.toLowerCase() === "cookie",
  )?.[1];
  if (cookieHeader) {
    // Scope cookies to the registrable domain (e.g. ".example.com"), not
    // host-only. Authenticated pages often 302 across sibling subdomains
    // (example.com -> app.example.com); a host-only cookie set for the
    // original host would not be sent to the redirect target, leaving the
    // request unauthenticated. The Cookie header carries no domain info, so
    // we apply the eTLD+1 — broad enough to follow the redirect, and these
    // are first-party cookies being returned to their own origin anyway.
    let cookieDomain: string | undefined;
    try {
      const host = new URL(url).hostname;
      const labels = host.split(".");
      cookieDomain = labels.length > 2 ? labels.slice(-2).join(".") : host;
    } catch {
      cookieDomain = undefined;
    }
    type SeedCookie = {
      name: string;
      value: string;
      url?: string;
      domain?: string;
      path?: string;
    };
    const cookies = cookieHeader
      .split(";")
      .map((pair) => pair.trim())
      .filter(Boolean)
      .map((pair): SeedCookie | null => {
        const eq = pair.indexOf("=");
        if (eq === -1) return null;
        const name = pair.slice(0, eq).trim();
        const value = pair.slice(eq + 1).trim();
        return cookieDomain
          ? { name, value, domain: `.${cookieDomain}`, path: "/" }
          : { name, value, url };
      })
      .filter((c): c is SeedCookie => c !== null);
    if (cookies.length > 0) {
      try {
        await requestContext.addCookies(cookies);
      } catch (error) {
        console.warn("Failed to seed cookies from Cookie header:", error);
      }
    }
  }

  // Remove user-agent (already applied at the context level) and cookie
  // (now seeded into the jar) before forwarding the rest verbatim.
  const filteredHeaders = Object.fromEntries(
    Object.entries(headers).filter(([k]) => {
      const lower = k.toLowerCase();
      return lower !== "user-agent" && lower !== "cookie";
    }),
  );
  if (Object.keys(filteredHeaders).length > 0) {
    await page.setExtraHTTPHeaders(filteredHeaders);
  }
};

export const createScrapeHandler =
  (deps: ScrapeRouteDeps) => async (req: Request, res: Response) => {
    // One deadline covers validation, queueing, pacing, setup and the scrape.
    const startedAt = Date.now();
    const {
      url,
      wait_after_load: requestedWaitAfterLoad,
      timeout: requestedTimeout,
      headers,
      check_selector,
      skip_tls_verification = false,
    }: UrlModel = req.body;
    const timing = parseScrapeTiming(requestedTimeout, requestedWaitAfterLoad);

    console.log(`================= Scrape Request =================`);
    console.log(`URL: ${url}`);
    console.log(
      `Wait After Load: ${timing.ok ? timing.waitAfterLoad : requestedWaitAfterLoad}`,
    );
    console.log(`Timeout: ${timing.ok ? timing.timeout : requestedTimeout}`);
    console.log(`Headers: ${headers ? JSON.stringify(headers) : "None"}`);
    console.log(`Check Selector: ${check_selector ? check_selector : "None"}`);
    console.log(`Skip TLS Verification: ${skip_tls_verification}`);
    console.log(`==================================================`);

    if (!url) {
      return res.status(400).json({ error: "URL is required" });
    }

    if (!isValidUrl(url)) {
      return res.status(400).json({ error: "Invalid URL" });
    }

    if (!timing.ok) {
      return res.status(400).json({ error: timing.error });
    }

    if (!deps.proxyConfigured) {
      console.warn(
        "⚠️ WARNING: No proxy server provided. Your IP address may be blocked.",
      );
    }

    const deadlineAt = startedAt + timing.timeout;
    const clientGone = new AbortController();
    res.on("close", () => {
      if (!res.writableFinished) clientGone.abort();
    });
    const { browsers } = deps;

    try {
      const userAgentOverride = headers
        ? Object.entries(headers).find(
            ([k]) => k.toLowerCase() === "user-agent",
          )?.[1]
        : undefined;

      await runScrapeLifecycle({
        deadlineAt,
        semaphore: deps.pageSemaphore,
        signal: clientGone.signal,
        prepare: async (remainingMs) => {
          await deps.assertSafeTargetUrl(url, deps.allowLocalTargets);
          remainingMs();
          if (!browsers.getBrowser()) await browsers.initializeBrowser();
        },
        paceStart: deps.paceStart,
        createContext: () =>
          browsers.createContext({
            allowLocalTargets: deps.allowLocalTargets,
            skipTlsVerification: skip_tls_verification,
            userAgent: userAgentOverride,
            deadlineAt,
          }),
        createPage: (bundle) => bundle.context.newPage(),
        closeContext: (bundle) => bundle.context.close(),
        closePage: (page) => page.close(),
        work: async (
          page,
          { context: requestContext, securityState },
          remainingMs,
        ) => {
          if (headers) {
            await applyScrapeHeaders(requestContext, page, url, headers);
          }
          // Playwright's own timeouts get a short grace past the deadline so
          // the lifecycle timer always reports the expiry (504).
          return scrapePage(
            page,
            url,
            "load",
            timing.waitAfterLoad,
            remainingMs() + PLAYWRIGHT_DEADLINE_GRACE_MS,
            check_selector,
            securityState,
          );
        },
        deliverResult: (result) => {
          const pageError =
            result.status !== 200 ? getError(result.status) : undefined;

          if (!pageError) {
            console.log(`✅ Scrape successful!`);
          } else {
            console.log(
              `🚨 Scrape failed with status code: ${result.status} ${pageError}`,
            );
          }

          res.json({
            content: result.content,
            pageStatusCode: result.status,
            contentType: result.contentType,
            url: result.url,
            ...(pageError && { pageError }),
          });
        },
      });
    } catch (error) {
      if (res.headersSent) {
        console.error("Scrape error after response delivery:", error);
        return;
      }
      if (error instanceof ScrapeClientGoneError) {
        console.warn("Scrape abandoned: client disconnected");
        return;
      }
      const failure = scrapeFailureResponse(error);
      if (failure.logAs) console.error(failure.logAs, error);
      res.status(failure.status).json(failure.body);
    }
  };
