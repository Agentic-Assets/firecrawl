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

// Every allocated public IPv6 address is in 2000::/3 (RFC 4291 global unicast).
const IPV6_GLOBAL_UNICAST = IPAddr.IPv6.parseCIDR("2000::/3");

// ipaddr.js reports these as "unicast" although they are not public
// destinations. ::/96 is the deprecated IPv4-compatible form (::a.b.c.d, so
// [::7f00:1] is 127.0.0.1): the whole block is refused rather than decoded,
// because no public service is reached that way. ::/96, fec0::/10 and
// 64:ff9b:1::/48 also fall outside 2000::/3; they are listed so the intent
// survives any change to that rule. 6to4 (2002::/16) and Teredo (2001::/32)
// are already non-unicast in ipaddr.js, so they are refused as whole blocks.
const NON_PUBLIC_IPV6 = [
  "::/96", // deprecated IPv4-compatible (RFC 4291 2.5.5.1)
  "fec0::/10", // deprecated site-local (RFC 3879)
  "64:ff9b:1::/48", // local-use NAT64 (RFC 8215)
  "3fff::/20", // documentation (RFC 9637)
].map((cidr) => IPAddr.IPv6.parseCIDR(cidr));

/** True when the address is anything but public unicast. */
function isInternalAddress(address: string): boolean {
  const parsed = IPAddr.parse(address);
  if (parsed.range() !== "unicast") return true;
  if (parsed.kind() !== "ipv6") return false;
  return (
    !parsed.match(IPV6_GLOBAL_UNICAST) ||
    NON_PUBLIC_IPV6.some((range) => parsed.match(range))
  );
}

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
  return addresses.some(isInternalAddress);
}
