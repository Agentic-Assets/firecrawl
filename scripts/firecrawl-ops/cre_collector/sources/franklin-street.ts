import { srcBuildout, BuildoutInventoryOpts } from "./buildout.js";
import { SourceResult, Tx } from "../types.js";

const FRANKLIN_STREET_PAGE = "https://www.franklinst.com/properties/";
const FRANKLIN_STREET_SALE_TOKEN = "a234450b432b2b2bebc1ace7e6f692e4489bde70";
const FRANKLIN_STREET_LEASE_TOKEN = "2f82fcd26667c4b0126d0084938ffa265f05fa4a";

export function franklinStreetBuildoutConfig(tx: Tx): {
  company: string;
  pluginKey: string;
  listingsPage: string;
  opts: BuildoutInventoryOpts;
} {
  return {
    company: "Franklin Street",
    pluginKey: tx === "lease" ? FRANKLIN_STREET_LEASE_TOKEN : FRANKLIN_STREET_SALE_TOKEN,
    listingsPage: FRANKLIN_STREET_PAGE,
    opts: {
      preferDirectJson: true,
      directReferer: FRANKLIN_STREET_PAGE,
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
