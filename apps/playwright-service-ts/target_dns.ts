import { lookup } from 'dns/promises';
import IPAddr from 'ipaddr.js';

export class TargetDnsUnavailableError extends Error {
  constructor() {
    super('Target DNS validation is unavailable');
  }
}

export async function isInternalHost(
  hostname: string,
  resolve: (host: string) => Promise<Array<{ address: string }>> = (host) =>
    lookup(host, { all: true }),
): Promise<boolean> {
  const host = hostname.toLowerCase().replace(/\.$/, '');
  if (!host) return true;
  let addresses: string[];
  if (IPAddr.isValid(host)) {
    addresses = [host];
  } else {
    try {
      addresses = (await resolve(host)).map((entry) => entry.address);
    } catch {
      // Fail closed, but do not misclassify resolver/resource failure as a private IP.
      throw new TargetDnsUnavailableError();
    }
    if (addresses.length === 0) throw new TargetDnsUnavailableError();
  }
  return addresses.some(
    (address) => IPAddr.parse(address).range() !== 'unicast',
  );
}
