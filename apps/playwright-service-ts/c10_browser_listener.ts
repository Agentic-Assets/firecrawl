import express, { type Express, type Request, type Response } from "express";
import {
  createHash,
  createPublicKey,
  randomUUID,
  timingSafeEqual,
} from "node:crypto";
import type { Browser, BrowserContext, Page } from "playwright";

import { withHardTimeout } from "./browser_resources";
import {
  C10_BROWSER_INTERNAL_PATH,
  C10_JLL_ADMISSION_LANE,
  C10SidecarCapabilityRegistry,
  parseC10SidecarInput,
  type C10AdmissionLane,
  publicKeyId,
  signC10Evidence,
} from "./c10_browser_internal";
import {
  c10TerminalEvidence,
  isC10SuccessfulBrowserResponse,
} from "./c10_browser_response";
import { executeC10BrowserPageFetch } from "./c10_browser_execution";
import {
  PermitLease,
  type PermitSource,
  awaitAllocation,
  releaseAfterClose,
} from "./permit_lease";

/**
 * Part of every card's window kept for closing its page and context, so a
 * navigation that times out at the work deadline is still cleaned up (and
 * its capacity returned) before the hard deadline.
 */
const C10_CLEANUP_RESERVE_MAX_MS = 2_000;

type C10ContextFactory = (
  options: Readonly<{ allowLocalTargets: boolean; skipTlsVerification?: boolean }>,
) => Promise<Readonly<{ context: BrowserContext }>>;

export type C10BrowserListenerConfig = Readonly<{
  coordinatorPublicKey: string | undefined;
  sidecarEvidencePrivateKey: string | undefined;
  hostTransportKey: string | undefined;
  port: string | undefined;
  profileSha256: string | undefined;
  /** Null for strict P0/P1; the reviewed lane name for JLL admission only. */
  admissionLane: C10AdmissionLane;
  admissionLaneValid: boolean;
  allowTestLocalTargets: boolean;
  hasAnyConfiguration: boolean;
  enabled: boolean;
}>;

export type C10BrowserListenerDependencies = Readonly<{
  config: C10BrowserListenerConfig;
  maxConcurrentPages: number;
  proxyServer: string | null;
  proxyCountry: string | undefined;
  pageSemaphore: PermitSource;
  getBrowser(): Browser | undefined;
  initializeBrowser(): Promise<void>;
  createContext: C10ContextFactory;
  assertSafeTargetUrl(
    url: string,
    allowLocalTargets: boolean,
  ): Promise<void>;
}>;

export type C10BrowserListener = Readonly<{
  app: Express;
  port: number | undefined;
  enabled: boolean;
}>;

function pemFromBase64(value: string | undefined): string | undefined {
  if (!value || !/^[A-Za-z0-9+/]+={0,2}$/.test(value) || value.length > 32_768) {
    return undefined;
  }
  try {
    const decoded = Buffer.from(value, "base64").toString("utf8");
    return decoded.startsWith("-----BEGIN ") && decoded.endsWith("-----\n")
      ? decoded
      : undefined;
  } catch {
    return undefined;
  }
}

/** Parse configuration once so the listener never reads ambient process state. */
export function readC10BrowserListenerConfig(
  environment: NodeJS.ProcessEnv,
): C10BrowserListenerConfig {
  const coordinatorPublicKey = pemFromBase64(
    environment.C10_COORDINATOR_PUBLIC_KEY_PEM_B64,
  );
  const sidecarEvidencePrivateKey = pemFromBase64(
    environment.C10_SIDECAR_EVIDENCE_PRIVATE_KEY_PEM_B64,
  );
  const hostTransportKey = environment.PLAYWRIGHT_HOST_TRANSPORT_V3_KEY;
  const port = environment.C10_BROWSER_INTERNAL_PORT;
  const profileSha256 = environment.C10_PROFILE_SHA256;
  const lane = environment.C10_ADMISSION_LANE;
  const admissionLane: C10AdmissionLane = lane === C10_JLL_ADMISSION_LANE ? C10_JLL_ADMISSION_LANE : null;
  return Object.freeze({
    coordinatorPublicKey,
    sidecarEvidencePrivateKey,
    hostTransportKey,
    port,
    profileSha256,
    admissionLane,
    admissionLaneValid: lane === undefined || lane === "" || lane === C10_JLL_ADMISSION_LANE,
    allowTestLocalTargets:
      environment.NODE_ENV === "test" &&
      environment.C10_BROWSER_INTERNAL_ALLOW_TEST_LOCAL_TARGETS === "true",
    hasAnyConfiguration: Boolean(
      environment.C10_COORDINATOR_PUBLIC_KEY_PEM_B64 ||
        environment.C10_SIDECAR_EVIDENCE_PRIVATE_KEY_PEM_B64 ||
        hostTransportKey ||
        port ||
        profileSha256,
    ),
    enabled: Boolean(
      coordinatorPublicKey &&
        sidecarEvidencePrivateKey &&
        hostTransportKey &&
        port &&
        profileSha256,
    ),
  });
}

export function assertC10BrowserListenerConfiguration(
  config: C10BrowserListenerConfig,
): void {
  if (config.hasAnyConfiguration && !config.enabled) {
    throw new Error(
      "C10 v3 requires coordinator public key, sidecar evidence private key, host transport key, profile identity, and port",
    );
  }
  if (config.enabled && (!config.port || !/^[1-9][0-9]{0,4}$/.test(config.port))) {
    throw new Error("C10_BROWSER_INTERNAL_PORT is invalid");
  }
  if (!config.admissionLaneValid) {
    throw new Error("C10_ADMISSION_LANE is not a reviewed admission lane");
  }
}

class C10PageLeasePool {
  private readonly available: Set<number>;

  constructor(private readonly capacity: number) {
    this.available = new Set(
      Array.from({ length: capacity }, (_, index) => index),
    );
  }

  acquire(): { leaseId: string; slot: number } {
    const slot = this.available.values().next().value;
    if (typeof slot !== "number") {
      throw new Error("C10 page slot is unavailable after semaphore admission");
    }
    this.available.delete(slot);
    return { leaseId: randomUUID(), slot };
  }

  release(slot: number): void {
    if (this.available.has(slot) || slot < 0 || slot >= this.capacity) {
      throw new Error("C10 page lease release is invalid");
    }
    this.available.add(slot);
  }

  availableCount(): number {
    return this.available.size;
  }
}

function validC10HostTransportKey(
  expectedKey: string | undefined,
  value: string | undefined,
): boolean {
  if (!expectedKey || !value) return false;
  const expected = Buffer.from(expectedKey, "utf8");
  const actual = Buffer.from(value, "utf8");
  return (
    expected.byteLength === actual.byteLength && timingSafeEqual(expected, actual)
  );
}

export function createC10BrowserListener(
  dependencies: C10BrowserListenerDependencies,
): C10BrowserListener {
  const { config } = dependencies;
  assertC10BrowserListenerConfiguration(config);
  const app = express();
  app.use(express.json({ limit: "600kb" }));

  if (
    !config.enabled ||
    !config.coordinatorPublicKey ||
    !config.sidecarEvidencePrivateKey ||
    !config.port
  ) {
    return Object.freeze({ app, port: undefined, enabled: false });
  }

  const coordinatorPublicKey = config.coordinatorPublicKey;
  const sidecarEvidencePrivateKey = config.sidecarEvidencePrivateKey;
  const capabilities = new C10SidecarCapabilityRegistry();
  const pageLeasePool = new C10PageLeasePool(dependencies.maxConcurrentPages);

  app.get("/health", (req: Request, res: Response) => {
    if (
      !validC10HostTransportKey(
        config.hostTransportKey,
        req.header("x-firecrawl-host-transport-key") ?? undefined,
      )
    ) {
      return res.sendStatus(401);
    }
    const health = {
      protocolVersion: 3,
      status: "healthy",
      transport: "docker-loopback-tcp",
      coordinatorKeyId: publicKeyId(coordinatorPublicKey),
      evidenceKeyId: publicKeyId(
        createPublicKey(sidecarEvidencePrivateKey)
          .export({ type: "spki", format: "pem" })
          .toString(),
      ),
      activePages:
        dependencies.maxConcurrentPages - pageLeasePool.availableCount(),
      configuredCapacity: dependencies.maxConcurrentPages,
      profileSha256: config.profileSha256,
      admissionLane: config.admissionLane,
      replayEntries: capabilities.size(),
    };
    return res.status(200).json({
      ...health,
      healthSignature: signC10Evidence(sidecarEvidencePrivateKey, health),
    });
  });

  app.post(C10_BROWSER_INTERNAL_PATH, async (req: Request, res: Response) => {
    if (
      !validC10HostTransportKey(
        config.hostTransportKey,
        req.header("x-firecrawl-host-transport-key") ?? undefined,
      )
    ) {
      return res.sendStatus(401);
    }
    let input;
    try {
      input = parseC10SidecarInput(req.body, { admissionLane: config.admissionLane });
    } catch {
      return res.status(400).json({ error: "Invalid internal C10 browser request" });
    }
    if (
      !capabilities.consume(
        coordinatorPublicKey,
        input,
        req.header("x-c10-browser-authorization") ?? undefined,
        Date.now(),
        { admissionLane: config.admissionLane },
      )
    ) {
      // Deliberately do not disclose whether the path, token, arm, or card was wrong.
      return res.sendStatus(404);
    }

    const queuedAt = Date.now();
    const deadlineAt = Math.min(
      queuedAt + input.card.timeoutMs,
      input.capability.expiresAtMs,
      input.capability.hostDeadlineAtMs,
    );
    const workDeadlineAt =
      deadlineAt -
      Math.min(C10_CLEANUP_RESERVE_MAX_MS, Math.floor((deadlineAt - queuedAt) / 4));
    const remaining = () => {
      const value = workDeadlineAt - Date.now();
      if (value < 1) throw new Error("C10 browser hard deadline expired");
      return value;
    };
    const bounded = <T>(
      operation: () => Promise<T>,
      closeLate?: (value: T) => Promise<unknown>,
    ): Promise<T> => {
      const timeoutMs = remaining();
      return withHardTimeout(
        operation(),
        timeoutMs,
        "C10 browser hard deadline expired",
        closeLate,
      );
    };
    let lease: PermitLease | null = null;
    let requestContext: BrowserContext | null = null;
    let page: Page | null = null;
    let evidence: Record<string, unknown> | null = null;
    let executionFailed = false;
    let cleanupConfirmed = false;
    try {
      await dependencies.assertSafeTargetUrl(
        input.card.browserBootstrapUrl,
        config.allowTestLocalTargets,
      );
      await dependencies.assertSafeTargetUrl(input.card.url, config.allowTestLocalTargets);
      remaining();
      if (!dependencies.getBrowser()) {
        await bounded(() => dependencies.initializeBrowser());
      }
      lease = await PermitLease.acquire(
        [{ source: dependencies.pageSemaphore, countsBrowser: true }],
        remaining,
      );
      if (
        input.capability.expiresAtMs <= Date.now() ||
        input.capability.hostDeadlineAtMs <= Date.now()
      ) {
        throw new Error("C10 capability expired while queued");
      }
      const pageLease = pageLeasePool.acquire();
      lease.add(() => pageLeasePool.release(pageLease.slot), true);
      const leaseStartMonotonicNs = process.hrtime.bigint().toString();
      const queueMs = Date.now() - queuedAt;
      const startedAt = Date.now();
      // A context that outlives the work deadline takes over the lease.
      const contextBundle = await awaitAllocation(
        lease,
        () =>
          dependencies.createContext({
            allowLocalTargets: config.allowTestLocalTargets,
            skipTlsVerification: config.allowTestLocalTargets,
          }),
        (allocation) => bounded(() => allocation),
        (lateBundle) => lateBundle.context.close(),
      );
      requestContext = contextBundle.context;
      const context = requestContext;
      // A late page belongs to the context, which the finally block closes.
      page = await bounded(() => context.newPage(), (latePage) => latePage.close());
      const requestPage = page;
      const cdp = await bounded(() => context.newCDPSession(requestPage));
      const network = { observed: false, cacheRead: false };
      cdp.on(
        "Network.responseReceived",
        (event: {
          response?: {
            fromDiskCache?: boolean;
            fromServiceWorker?: boolean;
          };
        }) => {
          network.observed = true;
          network.cacheRead ||=
            event.response?.fromDiskCache === true ||
            event.response?.fromServiceWorker === true;
        },
      );
      await bounded(() => cdp.send("Network.enable"));
      await bounded(() =>
        cdp.send("Network.setCacheDisabled", { cacheDisabled: true }),
      );
      const browserResponse = await bounded(() =>
        executeC10BrowserPageFetch(requestPage, input.card, workDeadlineAt),
      );
      if (!network.observed || network.cacheRead) {
        throw new Error("C10 browser cache evidence is unavailable or contradictory");
      }
      const body = Buffer.from(browserResponse.bodyBase64, "base64");
      if (body.byteLength > input.card.maxBytes) {
        throw new Error("C10 browser response exceeds its reviewed byte limit");
      }
      const challengeDetected = /(?:captcha|cf-chl|challenge-platform|access denied)/i.test(
        body.subarray(0, Math.min(body.byteLength, 256 * 1024)).toString("utf8"),
      );
      if (!isC10SuccessfulBrowserResponse(input.card, browserResponse, challengeDetected)) {
        throw new Error("C10 browser response does not meet the signed-success gate");
      }
      const proxyId = dependencies.proxyServer
        ? createHash("sha256").update(dependencies.proxyServer).digest("hex")
        : null;
      const leaseEndMonotonicNs = process.hrtime.bigint().toString();
      evidence = {
        protocolVersion: 3,
        binding: input.capability.binding,
        status: browserResponse.status,
        finalUrl: browserResponse.finalUrl,
        redirectCount:
          browserResponse.redirected || browserResponse.finalUrl !== input.card.url
            ? 1
            : 0,
        elapsedMs: Date.now() - startedAt,
        challengeDetected,
        contentType: browserResponse.contentType,
        bodyBase64: browserResponse.bodyBase64,
        jobId: randomUUID(),
        pageLease,
        leaseStartMonotonicNs,
        leaseEndMonotonicNs,
        observedActivePages:
          dependencies.maxConcurrentPages - pageLeasePool.availableCount(),
        configuredCapacity: dependencies.maxConcurrentPages,
        queueMs,
        proxy: {
          mode: dependencies.proxyServer ? "configured" : "direct",
          proxyId,
          country: dependencies.proxyCountry ?? null,
        },
        engineAttempt: {
          engine: "playwright-service",
          ordinal: 1,
          fallbackDisabled: true,
          fallbackUsed: false,
        },
        context: {
          ephemeral: true,
          storageState: "none",
          cache: "disabled-cdp-and-fetch-no-store",
        },
        cacheRead: false,
        cacheWrite: false,
      };
    } catch {
      console.error("C10 internal browser execution failed");
      executionFailed = true;
    } finally {
      if (lease === null) {
        // Nothing was admitted or allocated.
        cleanupConfirmed = true;
      } else if (lease.heldByRequest) {
        const remainingCleanupMs = deadlineAt - Date.now();
        const closePage = page ? () => page!.close() : null;
        const closeContext = requestContext ? () => requestContext!.close() : null;
        if (remainingCleanupMs > 0) {
          cleanupConfirmed = await releaseAfterClose(
            lease,
            closePage,
            closeContext,
            remainingCleanupMs,
          );
        } else {
          // Answer by the deadline, but still close what can be closed: the
          // background close releases the capacity or quarantines it.
          console.error("C10 v3 deadline exhausted before cleanup; closing in the background");
          lease.handOff();
          void releaseAfterClose(lease, closePage, closeContext);
        }
      }
    }
    const terminalEvidence = c10TerminalEvidence(
      evidence,
      executionFailed,
      cleanupConfirmed,
    );
    if (terminalEvidence) {
      return res.json({
        ...terminalEvidence,
        evidenceSignature: signC10Evidence(
          sidecarEvidencePrivateKey,
          terminalEvidence,
        ),
      });
    }
    console.error("C10 internal browser execution quarantined");
    return res.status(502).json({
      error: "C10 internal browser execution quarantined",
    });
  });

  return Object.freeze({
    app,
    port: Number(config.port),
    enabled: true,
  });
}
