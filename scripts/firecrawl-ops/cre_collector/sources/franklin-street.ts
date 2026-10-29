import { srcBuildout, BuildoutInventoryOpts, BUILDOUT_STABLE_INVENTORY_SORT } from "./buildout.js";
import { SourceResult, Tx } from "../types.js";
import { buildoutFeedFor } from "./buildout-feeds/index.js";

export function franklinStreetBuildoutConfig(tx: Tx): {
  company: string;
  pluginKey: string;
  listingsPage: string;
  opts: BuildoutInventoryOpts;
} {
  const feed = buildoutFeedFor("franklin-street", `franklin-street-${tx}`);
  if (!feed) throw new Error("missing Franklin Street Buildout feed coordinates");
  return {
    company: feed.company,
    pluginKey: feed.pluginKey,
    listingsPage: feed.listingsPage,
    opts: {
      preferDirectJson: true,
      inventorySort: BUILDOUT_STABLE_INVENTORY_SORT,
      directReferer: feed.listingsPage,
      pageConcurrency: 1,
      requireCompletePages: true,
      cacheSlug: `franklin-street-${tx}`,
      usePageCache: true,
      recoveryPasses: 1,
      recoveryCooldownMs: 15000,
      maxRecoveryPages: 30,
    },
  };
}

export async function srcFranklinStreet(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  const cfg = franklinStreetBuildoutConfig(tx);
  return srcBuildout(cfg.company, cfg.pluginKey, cfg.listingsPage, tx, max, monitor, cfg.opts);
}
