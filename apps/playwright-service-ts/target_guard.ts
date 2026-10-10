/**
 * SSRF guard for browser targets: the per-URL assertion used before and during
 * navigation, and the loopback forward proxy every browser context dials.
 */
import { Server, RequestError } from "proxy-chain";
import { isInternalHost, TargetDnsUnavailableError } from "./target_dns";

export class InsecureConnectionError extends Error {
  constructor(
    public readonly blockedUrl: string,
    reason: string,
  ) {
    super(`Blocked insecure target URL "${blockedUrl}": ${reason}`);
    this.name = "InsecureConnectionError";
  }
}

export type AssertSafeTargetUrl = (
  urlString: string,
  allowLocalTargets: boolean,
) => Promise<void>;

/**
 * Throws InsecureConnectionError for unsupported or private targets and
 * TargetDnsUnavailableError when the host cannot be classified.
 */
export const assertSafeTargetUrl: AssertSafeTargetUrl = async (
  urlString,
  allowLocalTargets,
) => {
  let parsedUrl: URL;
  try {
    parsedUrl = new URL(urlString);
  } catch {
    throw new InsecureConnectionError(urlString, "URL is invalid");
  }
  if (parsedUrl.protocol !== "http:" && parsedUrl.protocol !== "https:") {
    throw new InsecureConnectionError(
      urlString,
      `unsupported protocol "${parsedUrl.protocol}"`,
    );
  }
  if (!allowLocalTargets && (await isInternalHost(parsedUrl.hostname))) {
    throw new InsecureConnectionError(
      urlString,
      "resolves to a private/internal address",
    );
  }
};

export type UpstreamProxySettings = Readonly<{
  server: string | null;
  username: string | null;
  password: string | null;
}>;

const buildUpstreamProxyUrl = ({
  server,
  username,
  password,
}: UpstreamProxySettings): string | undefined => {
  if (!server) return undefined;
  const url = new URL(server.includes("://") ? server : `http://${server}`);
  if (username) url.username = username;
  if (password) url.password = password;
  return url.toString();
};

/** Loopback forward proxy that re-checks every hop, including redirects. */
export const startSsrfProxy = async (settings: {
  allowLocalTargets: boolean;
  upstream: UpstreamProxySettings;
}): Promise<number> => {
  const server = new Server({
    port: 0,
    host: "127.0.0.1",
    prepareRequestFunction: async ({ hostname }) => {
      if (!settings.allowLocalTargets) {
        let internal: boolean;
        try {
          internal = await isInternalHost(hostname);
        } catch (error) {
          if (error instanceof TargetDnsUnavailableError) {
            // Unclassifiable targets are never forwarded.
            throw new RequestError(
              "Blocked: target DNS validation is unavailable",
              502,
            );
          }
          throw error;
        }
        if (internal) {
          throw new RequestError(
            "Blocked: target resolves to a private/internal address",
            403,
          );
        }
      }
      return { upstreamProxyUrl: buildUpstreamProxyUrl(settings.upstream) };
    },
  });
  await server.listen();
  return server.port;
};
