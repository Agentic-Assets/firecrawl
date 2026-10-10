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
  const host = hostname.toLowerCase().replace(/\.$/, "");
  if (!host) return true;
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
