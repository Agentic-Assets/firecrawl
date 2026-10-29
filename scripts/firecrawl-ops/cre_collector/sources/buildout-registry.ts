import { registeredBuildoutTuples } from "./buildout-feeds/index.js";
import {
  BUILDOUT_STABLE_INVENTORY_SORT,
  type BuildoutInventoryOpts,
  type BuildoutDetailConfig,
} from "./buildout.js";
import type { SourceKey } from "../types.js";

/**
 * Historical public Buildout feeds recovered from commit 6245a7144.
 *
 * Keep this tuple as the source of truth for the key type. Registry parity
 * tests require every key to appear exactly once in REGISTERED_BUILDOUT_FIRMS.
 */
export const REGISTERED_BUILDOUT_SOURCE_KEYS = [
  "faris-lee",
  "fortis-net-lease",
  "unique-properties",
  "kiser-group",
  "pinnacle-rea",
  "cawley-chicago",
  "bradford-allen",
  "hudson-peters",
  "gibson-commercial",
  "leibsohn",
  "nai-hiffman",
  "nai-martens",
  "bull-realty",
  "tri-commercial",
  "berger-commercial",
  "nai-bergman",
  "nai-isaac",
  "trinity-partners",
  "metro-commercial",
  "33-realty",
  "nai-hallmark",
  "nai-plotkin",
  "greysteel",
  "nai-talcor",
  "nai-dominion",
] as const satisfies readonly SourceKey[];

export type RegisteredBuildoutSourceKey =
  (typeof REGISTERED_BUILDOUT_SOURCE_KEYS)[number];

export type RegisteredBuildoutFirm = {
  sourceKey: RegisteredBuildoutSourceKey;
  company: string;
  pluginKey: string;
  listingsPage: string;
  inventoryOpts: BuildoutInventoryOpts;
  /**
   * Optional because a public inventory feed can be supported before its
   * listing-side iframe coordinate is proven. Only definitions carrying a
   * validated config are registered for targeted detail enrichment.
   */
  detailConfig?: BuildoutDetailConfig;
};

function strictInventoryOpts(
  sourceKey: RegisteredBuildoutSourceKey,
  listingsPage: string
): BuildoutInventoryOpts {
  return {
    preferDirectJson: true,
    directReferer: listingsPage,
    inventorySort: BUILDOUT_STABLE_INVENTORY_SORT,
    pageConcurrency: 1,
    requireCompletePages: true,
    cacheSlug: sourceKey,
    usePageCache: true,
    recoveryPasses: 1,
    recoveryCooldownMs: 15000,
    maxRecoveryPages: 60,
  };
}

export function assertRegisteredBuildoutFirm(
  definition: RegisteredBuildoutFirm
): void {
  if (!REGISTERED_BUILDOUT_SOURCE_KEYS.includes(definition.sourceKey)) {
    throw new Error(`unknown registered Buildout source key ${definition.sourceKey}`);
  }
  if (!definition.company.trim()) {
    throw new Error(`${definition.sourceKey}: Buildout company must be nonempty`);
  }
  if (!/^[a-f0-9]{40}$/.test(definition.pluginKey)) {
    throw new Error(`${definition.sourceKey}: Buildout plugin key must be 40 lowercase hex characters`);
  }
  let page: URL;
  try {
    page = new URL(definition.listingsPage);
  } catch {
    throw new Error(`${definition.sourceKey}: Buildout listings page must be an absolute URL`);
  }
  if (
    page.protocol !== "https:" ||
    page.username ||
    page.password ||
    page.port ||
    page.hash ||
    page.search
  ) {
    throw new Error(`${definition.sourceKey}: Buildout listings page must be a clean HTTPS URL`);
  }
  if (definition.inventoryOpts.cacheSlug !== definition.sourceKey) {
    throw new Error(`${definition.sourceKey}: Buildout cache namespace must equal the source key`);
  }
  if (definition.inventoryOpts.directReferer !== definition.listingsPage) {
    throw new Error(`${definition.sourceKey}: Buildout direct referer must equal the listings page`);
  }
  if (definition.detailConfig) {
    const host = definition.detailConfig.host.toLowerCase();
    if (
      !/^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/.test(host) ||
      host.includes("..") ||
      host !== page.hostname.toLowerCase()
    ) {
      throw new Error(
        `${definition.sourceKey}: Buildout detail host must exactly match the listings-page host`
      );
    }
    if (definition.detailConfig.allowHttpListingInput !== undefined &&
        (definition.sourceKey !== "leibsohn" || definition.detailConfig.allowHttpListingInput !== true)) {
      throw new Error(`${definition.sourceKey}: Buildout HTTP listing input is not admitted`);
    }
    const listingHosts = definition.detailConfig.listingHosts;
    if (
      listingHosts.length === 0 ||
      new Set(listingHosts).size !== listingHosts.length ||
      listingHosts.some(
        (listingHost) =>
          listingHost !== listingHost.toLowerCase() ||
          !/^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/.test(listingHost) ||
          listingHost.includes("..")
      ) ||
      !listingHosts.includes(page.hostname.toLowerCase())
    ) {
      throw new Error(
        `${definition.sourceKey}: Buildout detail listing hosts must be unique lowercase hosts and include the listings-page host`
      );
    }
    if (definition.detailConfig.pluginKey !== definition.pluginKey) {
      throw new Error(
        `${definition.sourceKey}: Buildout detail plugin key must equal the inventory plugin key`
      );
    }
    if (definition.detailConfig.company !== definition.company) {
      throw new Error(
        `${definition.sourceKey}: Buildout detail company must equal the registry company`
      );
    }
  }
}

function firm(
  sourceKey: RegisteredBuildoutSourceKey,
  company: string,
  pluginKey: string,
  listingsPage: string,
  detailHost?: string
): RegisteredBuildoutFirm {
  const definition: RegisteredBuildoutFirm = {
    sourceKey,
    company,
    pluginKey,
    listingsPage,
    inventoryOpts: strictInventoryOpts(sourceKey, listingsPage),
    ...(detailHost
      ? {
          detailConfig: {
            pluginKey,
            host: detailHost,
            listingHosts: [detailHost],
            company,
            ...(sourceKey === "leibsohn" ? { allowHttpListingInput: true } : {}),
          },
        }
      : {}),
  };
  assertRegisteredBuildoutFirm(definition);
  return definition;
}

// Shared with the current-inventory admission validator. Public provider coordinates,
// not credentials; the typed key tuple and firm validation remain authoritative.
export const REGISTERED_BUILDOUT_FIRMS: readonly RegisteredBuildoutFirm[] = registeredBuildoutTuples.map(
  ([sourceKey, company, pluginKey, listingsPage, detailHost]) =>
    firm(sourceKey as RegisteredBuildoutSourceKey, company, pluginKey, listingsPage, detailHost)
);


if (REGISTERED_BUILDOUT_FIRMS.length !== REGISTERED_BUILDOUT_SOURCE_KEYS.length) {
  throw new Error("registered Buildout tuple and firm definitions differ in length");
}
if (
  new Set(REGISTERED_BUILDOUT_FIRMS.map(({ sourceKey }) => sourceKey)).size !==
  REGISTERED_BUILDOUT_SOURCE_KEYS.length
) {
  throw new Error("registered Buildout source keys must be unique");
}
if (
  new Set(REGISTERED_BUILDOUT_FIRMS.map(({ pluginKey }) => pluginKey)).size !==
  REGISTERED_BUILDOUT_FIRMS.length
) {
  throw new Error("registered Buildout plugin keys must be unique");
}

const REGISTERED_BUILDOUT_FIRM_BY_SOURCE = new Map<
  RegisteredBuildoutSourceKey,
  RegisteredBuildoutFirm
>(
  REGISTERED_BUILDOUT_FIRMS.map((definition) => [
    definition.sourceKey,
    definition,
  ])
);

export function registeredBuildoutFirmFor(
  sourceKey: SourceKey
): RegisteredBuildoutFirm | null {
  return (
    REGISTERED_BUILDOUT_FIRM_BY_SOURCE.get(
      sourceKey as RegisteredBuildoutSourceKey
    ) ?? null
  );
}
