// Default-off public JLL detail transport. No asset downloads or provider writes.
import * as cheerio from "cheerio";
import { lookup } from "node:dns/promises";
import { request as httpsRequest } from "node:https";
import { BlockList, isIP } from "node:net";
import TurndownService from "turndown";
import type { ScrapedDoc } from "../types.js";

export const JLL_DIRECT_MAX_BYTES = 5 * 1024 * 1024;
export const JLL_DIRECT_START_INTERVAL_MS = 150;
const MAX_REDIRECTS = 3;

// One process-wide gate for direct sockets, including redirects. Measure the
// next start from actual dispatch: delayed timers cannot compress spacing.
// Cancelled waiters are removed immediately and never open a late socket.
class JllDirectStartPacer {
  private nextStart = 0;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private queue: Array<{ dispatch: () => void; signal: AbortSignal; cancel: () => void }> = [];

  start<T>(signal: AbortSignal, operation: () => Promise<T>): Promise<T> {
    if (signal.aborted) return Promise.reject(signal.reason);
    return new Promise<T>((resolve, reject) => {
      const entry = {
        signal,
        cancel: () => {
          this.queue = this.queue.filter((queued) => queued !== entry);
          signal.removeEventListener("abort", entry.cancel);
          reject(signal.reason);
          this.drain();
        },
        dispatch: () => {
          signal.removeEventListener("abort", entry.cancel);
          try { signal.throwIfAborted(); resolve(operation()); }
          catch (error) { reject(error); }
        },
      };
      signal.addEventListener("abort", entry.cancel, { once: true });
      this.queue.push(entry);
      this.drain();
    });
  }

  private drain(): void {
    if (this.timer) { clearTimeout(this.timer); this.timer = undefined; }
    if (!this.queue.length) return;
    const remaining = this.nextStart - Date.now();
    if (remaining > 0) {
      this.timer = setTimeout(() => this.drain(), remaining);
      return;
    }
    const entry = this.queue.shift()!;
    entry.dispatch();
    this.nextStart = Date.now() + JLL_DIRECT_START_INTERVAL_MS;
    this.drain();
  }
}
const directStartPacer = new JllDirectStartPacer();
const blockedV4 = new BlockList();
for (const [network, prefix] of [
  ["0.0.0.0", 8], ["10.0.0.0", 8], ["100.64.0.0", 10], ["127.0.0.0", 8],
  ["169.254.0.0", 16], ["172.16.0.0", 12], ["192.0.0.0", 24],
  ["192.0.2.0", 24], ["192.168.0.0", 16], ["198.18.0.0", 15],
  ["198.51.100.0", 24], ["203.0.113.0", 24], ["224.0.0.0", 4], ["240.0.0.0", 4],
] as Array<[string, number]>) blockedV4.addSubnet(network, prefix, "ipv4");
const globalV6 = new BlockList();
globalV6.addSubnet("2000::", 3, "ipv6");
const blockedV6 = new BlockList();
for (const [network, prefix] of [
  ["2001::", 23], ["2001:db8::", 32], ["2002::", 16], ["3fff::", 20],
] as Array<[string, number]>) blockedV6.addSubnet(network, prefix, "ipv6");

export function isJllPublicAddress(address: string): boolean {
  const family = isIP(address);
  return family === 4 ? !blockedV4.check(address, "ipv4")
    : family === 6 && globalV6.check(address, "ipv6") && !blockedV6.check(address, "ipv6");
}

export function assertJllDirectUrl(value: string | URL): URL {
  const url = new URL(value);
  if (url.protocol !== "https:" || url.hostname !== "property.jll.com" || url.port
      || url.username || url.password || !/^\/listings\/[^/]+\/?$/.test(url.pathname)) {
    throw new Error("JLL direct detail requires an approved HTTPS listing URL");
  }
  url.hash = "";
  return url;
}

export type JllPinnedResponse = {
  status: number;
  location: string | null;
  contentType: string;
  body: string;
};
export type JllDirectDependencies = {
  resolveHost?: (hostname: string) => Promise<string[]>;
  requestPinned?: (url: URL, address: string, signal: AbortSignal) => Promise<JllPinnedResponse>;
};

// The TLS hostname stays property.jll.com; DNS is resolved once and the socket
// lookup is pinned to the validated result. Disable pooling so an old connection
// cannot silently bypass the address admitted for this request.
export function requestJllPinned(
  url: URL,
  address: string,
  signal: AbortSignal,
  request: typeof httpsRequest = httpsRequest
): Promise<JllPinnedResponse> {
  return new Promise((resolve, reject) => {
    const req = request(url, {
      agent: false,
      signal,
      headers: {
        accept: "text/html,application/xhtml+xml",
        "accept-encoding": "identity",
        "user-agent": "Mozilla/5.0 CRE collector",
        "cache-control": "no-cache",
        pragma: "no-cache",
      },
      lookup: ((_hostname: string, options: any, callback: Function) => {
        const family = isIP(address);
        if (options?.all) callback(null, [{ address, family }]);
        else callback(null, address, family);
      }) as any,
    }, (res) => {
      const chunks: Buffer[] = [];
      let bytes = 0;
      const fail = (error: Error) => { reject(error); res.destroy(); req.destroy(); };
      res.on("error", reject);
      res.on("aborted", () => reject(new Error("JLL direct detail body was interrupted")));
      if (Number(res.headers["content-length"]) > JLL_DIRECT_MAX_BYTES) {
        fail(new Error("JLL direct detail exceeded 5 MiB"));
        return;
      }
      if (res.headers["content-encoding"] && res.headers["content-encoding"] !== "identity") {
        fail(new Error("JLL direct detail returned unsupported content encoding"));
        return;
      }
      res.on("data", (chunk: Buffer | string) => {
        const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        bytes += buffer.length;
        if (bytes > JLL_DIRECT_MAX_BYTES) {
          fail(new Error("JLL direct detail exceeded 5 MiB"));
          return;
        }
        chunks.push(buffer);
      });
      res.on("end", () => resolve({
        status: res.statusCode ?? 0,
        location: res.headers.location ?? null,
        contentType: String(res.headers["content-type"] ?? ""),
        body: Buffer.concat(chunks).toString("utf8"),
      }));
    });
    req.on("error", reject);
    req.end();
  });
}

function absoluteUrl(value: string, base: string): string {
  try { return new URL(value, base).toString(); } catch { return value; }
}

// Match scrapeDoc's full-page capture, including every structured attribute
// selector. Like Avison Young, use Turndown for durable direct HTML Markdown;
// retain tables as HTML because plain Turndown would discard their structure.
export function jllDirectHtmlDoc(rawHtml: string, url: string): ScrapedDoc {
  const $ = cheerio.load(rawHtml);
  const values = (selector: string, attribute: string) => $(selector)
    .map((_, el) => $(el).attr(attribute)).get()
    .filter((value): value is string => Boolean(value))
    .map((value) => absoluteUrl(value, url));
  const attributes = [
    { selector: "div[component=video]", attribute: "url" },
    { selector: "iframe", attribute: "src" },
    { selector: "a", attribute: "href" },
    { selector: "video source", attribute: "src" },
    { selector: "[data-video-url]", attribute: "data-video-url" },
  ].map((entry) => ({ ...entry, values: values(entry.selector, entry.attribute) }));
  const links = values("a[href]", "href");
  const images = values("img[src]", "src");
  const content = $("body").clone();
  content.find("script, style, noscript").remove();
  for (const [selector, attribute] of [["a[href]", "href"], ["img[src]", "src"]]) {
    content.find(selector).each((_, el) => {
      $(el).attr(attribute, absoluteUrl($(el).attr(attribute) ?? "", url));
    });
  }
  const converter = new TurndownService({ headingStyle: "atx", bulletListMarker: "-", codeBlockStyle: "fenced" });
  converter.keep(["table"]);
  const markdown = converter.turndown(content.html() ?? "").trim();
  if (!markdown) throw new Error("JLL direct detail has no durable page Markdown");
  return {
    rawHtml, markdown, links, images, attributes,
    metadata: { statusCode: 200, sourceURL: url, url, transport: "direct_http" },
  };
}

export async function fetchJllDirectDoc(
  url: string,
  timeoutMs = 15000,
  dependencies: JllDirectDependencies = {}
): Promise<ScrapedDoc> {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 60000) {
    throw new Error("JLL direct detail deadline must be positive and at most 60000ms");
  }
  const controller = new AbortController();
  const deadlineError = new Error("JLL direct detail deadline exceeded");
  let timer: ReturnType<typeof setTimeout>;
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => { controller.abort(); reject(deadlineError); }, timeoutMs);
  });
  const resolveHost = dependencies.resolveHost ?? (async (hostname: string) =>
    (await lookup(hostname, { all: true, verbatim: true })).map((entry) => entry.address));
  const requestPinned = dependencies.requestPinned ?? requestJllPinned;
  const run = async () => {
    let current = assertJllDirectUrl(url);
    for (let redirect = 0; redirect <= MAX_REDIRECTS; redirect++) {
      controller.signal.throwIfAborted();
      current = assertJllDirectUrl(current);
      const addresses = await resolveHost(current.hostname);
      controller.signal.throwIfAborted();
      if (!addresses.length || addresses.some((address) => !isJllPublicAddress(address))) {
        throw new Error("JLL direct detail DNS resolved a non-public address");
      }
      const response = await directStartPacer.start(controller.signal, () =>
        requestPinned(current, addresses[0]!, controller.signal));
      controller.signal.throwIfAborted();
      if (Buffer.byteLength(response.body, "utf8") > JLL_DIRECT_MAX_BYTES) {
        throw new Error("JLL direct detail exceeded 5 MiB");
      }
      if ([301, 302, 303, 307, 308].includes(response.status)) {
        if (!response.location || redirect === MAX_REDIRECTS) {
          throw new Error("JLL direct detail redirect missing Location or exceeded limit");
        }
        current = assertJllDirectUrl(new URL(response.location, current));
        continue;
      }
      if (response.status < 200 || response.status >= 300) {
        throw new Error(`JLL direct detail returned HTTP ${response.status}`);
      }
      if (!/^(text\/html|application\/xhtml\+xml)(?:;|$)/i.test(response.contentType)) {
        throw new Error("JLL direct detail returned non-HTML content");
      }
      return jllDirectHtmlDoc(response.body, current.toString());
    }
    throw new Error("JLL direct detail exhausted redirects");
  };
  try { return await Promise.race([run(), deadline]); }
  finally { clearTimeout(timer!); controller.abort(); }
}
