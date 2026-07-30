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
          },
        }
      : {}),
  };
  assertRegisteredBuildoutFirm(definition);
  return definition;
}

export const REGISTERED_BUILDOUT_FIRMS = [
  firm(
    "faris-lee",
    "Faris Lee Investments",
    "de89d4f043da3999d293e1adcfd541bf2530acca",
    "https://www.farislee.com/listings/",
    "www.farislee.com"
  ),
  firm(
    "fortis-net-lease",
    "Fortis Net Lease",
    "8c286e4a49fdc706359ab9c041e0db1465de1fcf",
    "https://www.fortisnetlease.com/net-lease-properties/",
    "www.fortisnetlease.com"
  ),
  firm(
    "unique-properties",
    "Unique Properties",
    "43994fa6c8bc167acf6e799d1ecd08173254b362",
    "https://www.uniqueprop.com/",
    "www.uniqueprop.com"
  ),
  firm(
    "kiser-group",
    "Kiser Group",
    "f9624a304f0b834544c60c666a56ca16fcf29a1f",
    "https://www.kisergroup.com/",
    "www.kisergroup.com"
  ),
  firm(
    "pinnacle-rea",
    "Pinnacle Real Estate Advisors",
    "53aeead9dc03d2337633a409497ff7976f68d56c",
    "https://www.pinnaclerea.com/",
    "www.pinnaclerea.com"
  ),
  firm(
    "cawley-chicago",
    "Cawley Chicago",
    "408316c565e1efe74e56779fffe3baa3fdc1f3cf",
    "https://www.cawleychicago.com/",
    "www.cawleychicago.com"
  ),
  firm(
    "bradford-allen",
    "Bradford Allen",
    "f2c7e5eec6ebe7de1f4a0b261bd9a04d715ca1e1",
    "https://www.bradfordallen.com/",
    "www.bradfordallen.com"
  ),
  firm(
    "hudson-peters",
    "Hudson Peters Commercial",
    "fb2068dac489e1dacd436ebe03523aed6df9fe2e",
    "https://www.hudsonpeters.com/",
    "www.hudsonpeters.com"
  ),
  firm(
    "gibson-commercial",
    "Gibson Commercial Real Estate",
    "cf76c48a3374831d301742075017a4b5e88642bc",
    "https://www.gibsoncre.com/",
    "www.gibsoncre.com"
  ),
  firm(
    "leibsohn",
    "Leibsohn & Co",
    "9be8516e186ae4deb9ee10eafda9478aca7ffe68",
    "https://www.leibsohn.com/",
    "www.leibsohn.com"
  ),
  firm(
    "nai-hiffman",
    "NAI Hiffman",
    "783881343a019c17532413fa9b120e61d47c2ae3",
    "https://www.hiffman.com/",
    "www.hiffman.com"
  ),
  firm(
    "nai-martens",
    "NAI Martens",
    "6351fc3e892388a1a2dbf1bdc7f65fd1ac144231",
    "https://www.naimartens.com/",
    "www.naimartens.com"
  ),
  firm(
    "bull-realty",
    "Bull Realty",
    "6e2064ba71e11d85d50740c87a9372ef9c961a46",
    "https://www.bullrealty.com/",
    "www.bullrealty.com"
  ),
  firm(
    "tri-commercial",
    "TRI Commercial",
    "4d24ff217c26907aaaa12bb0837e451e568a61e4",
    "https://www.tricommercial.com/",
    "www.tricommercial.com"
  ),
  firm(
    "berger-commercial",
    "Berger Commercial Real Estate",
    "b1a0682147c41af0dc0ea1af91664ab8ea766aa9",
    "https://www.bergercommercial.com/",
    "www.bergercommercial.com"
  ),
  firm(
    "nai-bergman",
    "NAI Bergman",
    "70e208db445d84be6d7c074ee0108373ccf755a8",
    "https://www.naibergman.com/",
    "www.naibergman.com"
  ),
  firm(
    "nai-isaac",
    "NAI Isaac",
    "9ad3babf4f98852f6ed9b0b9db30388bb7e07c5a",
    "https://www.naiisaac.com/",
    "www.naiisaac.com"
  ),
  firm(
    "trinity-partners",
    "Trinity Partners",
    "1c2d2e5340b1956e6a900d94c4dd3b41b69c2af9",
    "https://www.trinity-partners.com/",
    "www.trinity-partners.com"
  ),
  firm(
    "metro-commercial",
    "Metro Commercial",
    "45a0bd5e3569b2b9d10a3bd88f93fda41ba238f6",
    "https://www.metrocommercial.com/",
    "www.metrocommercial.com"
  ),
  firm(
    "33-realty",
    "33 Realty",
    "5bdefd87a602a896a48f635e07a6724215ed764e",
    "https://33realty.com/",
    "33realty.com"
  ),
  firm(
    "nai-hallmark",
    "NAI Hallmark",
    "f883dbd9ac44b7702c0c0bfd4722925868f23ecb",
    "https://www.naihallmark.com/",
    "www.naihallmark.com"
  ),
  firm(
    "nai-plotkin",
    "NAI Plotkin",
    "f3a493d487cf05648f54bc6264231beb9f4cd176",
    "https://www.naiplotkin.com/",
    "www.naiplotkin.com"
  ),
  firm(
    "greysteel",
    "Greysteel",
    "a6dbbaba3cc0ba7d1fbc587e9f06c953cebed964",
    "https://www.greysteel.com/",
    "www.greysteel.com"
  ),
  firm(
    "nai-talcor",
    "NAI TALCOR",
    "b9b19d2a3f66dfc3bb532e8c5db7399f4db33349",
    "https://www.naitalcor.com/",
    "www.naitalcor.com"
  ),
  firm(
    "nai-dominion",
    "NAI Dominion",
    "6a78703278580ac43114429ef6f4a0d484167434",
    "https://www.naidominion.com/",
    "www.naidominion.com"
  ),
] as const satisfies readonly RegisteredBuildoutFirm[];

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
