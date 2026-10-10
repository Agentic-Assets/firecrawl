import { lookup } from "dns/promises";
import IPAddr from "ipaddr.js";

/**
 * The target host could not be resolved, so it could not be classified.
 * Callers must still refuse the request (fail closed), but may report it as a
 * retryable availability failure instead of a blocked private destination.
 */
export class TargetDnsUnavailableError extends Error {
  constructor() {
    super("Target DNS validation is unavailable");
    this.name = "TargetDnsUnavailableError";
  }
}

export type TargetResolver = (
  host: string,
) => Promise<Array<{ address: string }>>;

const systemResolver: TargetResolver = (host) => lookup(host, { all: true });

/**
 * Returns true when any resolved address is not public unicast. Throws
 * TargetDnsUnavailableError when resolution fails or returns no addresses;
 * it never returns false for a host it could not classify.
 */
export async function isInternalHost(
  hostname: string,
  resolve: TargetResolver = systemResolver,
): Promise<boolean> {
  const lowered = hostname.toLowerCase();
  // WHATWG URL (used by the route guard, /scrape and the SSRF proxy) keeps
  // the brackets on IPv6 literals: new URL("http://[::1]/").hostname is
  // "[::1]". Strip exactly one pair so the literal is classified, never
  // resolved. A bracketed value that is not an IPv6 address is refused.
  const bracketed = lowered.startsWith("[") && lowered.endsWith("]");
  const host = bracketed
    ? lowered.slice(1, -1)
    : lowered.replace(/\.$/, "");
  if (!host) return true;
  if (bracketed && !IPAddr.IPv6.isValid(host)) return true;
  let addresses: string[];
  if (IPAddr.isValid(host)) {
    addresses = [host];
  } else {
    try {
      addresses = (await resolve(host)).map((entry) => entry.address);
    } catch {
      throw new TargetDnsUnavailableError();
    }
    if (addresses.length === 0) throw new TargetDnsUnavailableError();
  }
  return addresses.some(
    (address) => IPAddr.parse(address).range() !== "unicast",
  );
}
