/** Source-owned public Buildout coordinates shared by collection and private capture. */
import { readFileSync } from "node:fs";
import { SOURCE_KEYS } from "../../types.js";

export type BuildoutFeedCoordinates = {
  sourceKey: string; company: string; pluginKey: string; listingsPage: string; detailHost: string; feedKey: string;
};
export type RegisteredBuildoutTuple = readonly [string, string, string, string, string];

function readTuples(path: URL, width: number, count: number): string[][] {
  const value: unknown = JSON.parse(readFileSync(path, "utf8"));
  if (!Array.isArray(value) || value.length !== count || value.some(
    (row) => !Array.isArray(row) || row.length !== width || row.some((item) => typeof item !== "string")
  )) throw new Error("invalid Buildout feed coordinate tuples");
  return value as string[][];
}

const registered = readTuples(new URL("../../data/cre-buildout-sources.json", import.meta.url), 5, 25);
const core = readTuples(new URL("../../data/cre-buildout-core-feeds.json", import.meta.url), 6, 4);
export const registeredBuildoutTuples: readonly RegisteredBuildoutTuple[] = Object.freeze(registered.map((row) =>
  Object.freeze([row[0]!, row[1]!, row[2]!, row[3]!, row[4]!] as const)));

const feeds: BuildoutFeedCoordinates[] = [
  ...registered.map(([sourceKey, company, pluginKey, listingsPage, detailHost]) => ({
    sourceKey: sourceKey!, company: company!, pluginKey: pluginKey!, listingsPage: listingsPage!,
    detailHost: detailHost!, feedKey: sourceKey!,
  })),
  ...core.map(([sourceKey, company, pluginKey, listingsPage, detailHost, feedKey]) => ({
    sourceKey: sourceKey!, company: company!, pluginKey: pluginKey!, listingsPage: listingsPage!,
    detailHost: detailHost!, feedKey: feedKey!,
  })),
];

for (const feed of feeds) {
  const { sourceKey, company, pluginKey, listingsPage, detailHost, feedKey } = feed;
  let url: URL;
  try { url = new URL(listingsPage); } catch { throw new Error("invalid Buildout feed listing URL"); }
  if (!SOURCE_KEYS.includes(sourceKey as (typeof SOURCE_KEYS)[number]) || !company.trim()
      || !/^[a-f0-9]{40}$/.test(pluginKey) || url.protocol !== "https:" || url.username || url.password
      || url.port || url.hash || url.search || !/^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/.test(detailHost)
      || detailHost.includes("..") || url.hostname !== detailHost
      || (sourceKey === "franklin-street"
        ? !["franklin-street-sale", "franklin-street-lease"].includes(feedKey)
        : feedKey !== sourceKey)) throw new Error("invalid Buildout feed coordinates");
}
if (new Set(feeds.map((feed) => feed.feedKey)).size !== feeds.length
    || new Set(feeds.map((feed) => feed.pluginKey)).size !== feeds.length
    || new Set(registered.map((row) => row[0])).size !== 25
    || new Set(core.map((row) => row[5])).size !== 4
    || new Set(core.map((row) => row[0])).size !== 3
    || !["svn", "lee-associates", "franklin-street-sale", "franklin-street-lease"].every(
      (key) => core.some((row) => row[5] === key))) throw new Error("duplicate or incomplete Buildout feed registry");

export const allBuildoutFeeds: readonly Readonly<BuildoutFeedCoordinates>[] = Object.freeze(feeds.map((feed) => Object.freeze(feed)));
export function buildoutFeedFor(sourceKey: string, feedKey = sourceKey): Readonly<BuildoutFeedCoordinates> | null {
  return allBuildoutFeeds.find((feed) => feed.sourceKey === sourceKey && feed.feedKey === feedKey) ?? null;
}
