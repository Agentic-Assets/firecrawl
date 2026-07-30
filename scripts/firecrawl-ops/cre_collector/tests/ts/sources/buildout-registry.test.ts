import test from "node:test";
import assert from "node:assert/strict";
import {
  BUILDOUT_STABLE_INVENTORY_SORT,
  buildoutDetailIframeUrl,
} from "../../../sources/buildout.js";
import {
  REGISTERED_BUILDOUT_FIRMS,
  REGISTERED_BUILDOUT_SOURCE_KEYS,
  assertRegisteredBuildoutFirm,
  registeredBuildoutFirmFor,
  type RegisteredBuildoutFirm,
} from "../../../sources/buildout-registry.js";

const EXPECTED_FIRMS = [
  ["faris-lee", "Faris Lee Investments", "de89d4f043da3999d293e1adcfd541bf2530acca", "https://www.farislee.com/listings/"],
  ["fortis-net-lease", "Fortis Net Lease", "8c286e4a49fdc706359ab9c041e0db1465de1fcf", "https://www.fortisnetlease.com/net-lease-properties/"],
  ["unique-properties", "Unique Properties", "43994fa6c8bc167acf6e799d1ecd08173254b362", "https://www.uniqueprop.com/"],
  ["kiser-group", "Kiser Group", "f9624a304f0b834544c60c666a56ca16fcf29a1f", "https://www.kisergroup.com/"],
  ["pinnacle-rea", "Pinnacle Real Estate Advisors", "53aeead9dc03d2337633a409497ff7976f68d56c", "https://www.pinnaclerea.com/"],
  ["cawley-chicago", "Cawley Chicago", "408316c565e1efe74e56779fffe3baa3fdc1f3cf", "https://www.cawleychicago.com/"],
  ["bradford-allen", "Bradford Allen", "f2c7e5eec6ebe7de1f4a0b261bd9a04d715ca1e1", "https://www.bradfordallen.com/"],
  ["hudson-peters", "Hudson Peters Commercial", "fb2068dac489e1dacd436ebe03523aed6df9fe2e", "https://www.hudsonpeters.com/"],
  ["gibson-commercial", "Gibson Commercial Real Estate", "cf76c48a3374831d301742075017a4b5e88642bc", "https://www.gibsoncre.com/"],
  ["leibsohn", "Leibsohn & Co", "9be8516e186ae4deb9ee10eafda9478aca7ffe68", "https://www.leibsohn.com/"],
  ["nai-hiffman", "NAI Hiffman", "783881343a019c17532413fa9b120e61d47c2ae3", "https://www.hiffman.com/"],
  ["nai-martens", "NAI Martens", "6351fc3e892388a1a2dbf1bdc7f65fd1ac144231", "https://www.naimartens.com/"],
  ["bull-realty", "Bull Realty", "6e2064ba71e11d85d50740c87a9372ef9c961a46", "https://www.bullrealty.com/"],
  ["tri-commercial", "TRI Commercial", "4d24ff217c26907aaaa12bb0837e451e568a61e4", "https://www.tricommercial.com/"],
  ["berger-commercial", "Berger Commercial Real Estate", "b1a0682147c41af0dc0ea1af91664ab8ea766aa9", "https://www.bergercommercial.com/"],
  ["nai-bergman", "NAI Bergman", "70e208db445d84be6d7c074ee0108373ccf755a8", "https://www.naibergman.com/"],
  ["nai-isaac", "NAI Isaac", "9ad3babf4f98852f6ed9b0b9db30388bb7e07c5a", "https://www.naiisaac.com/"],
  ["trinity-partners", "Trinity Partners", "1c2d2e5340b1956e6a900d94c4dd3b41b69c2af9", "https://www.trinity-partners.com/"],
  ["metro-commercial", "Metro Commercial", "45a0bd5e3569b2b9d10a3bd88f93fda41ba238f6", "https://www.metrocommercial.com/"],
  ["33-realty", "33 Realty", "5bdefd87a602a896a48f635e07a6724215ed764e", "https://33realty.com/"],
  ["nai-hallmark", "NAI Hallmark", "f883dbd9ac44b7702c0c0bfd4722925868f23ecb", "https://www.naihallmark.com/"],
  ["nai-plotkin", "NAI Plotkin", "f3a493d487cf05648f54bc6264231beb9f4cd176", "https://www.naiplotkin.com/"],
  ["greysteel", "Greysteel", "a6dbbaba3cc0ba7d1fbc587e9f06c953cebed964", "https://www.greysteel.com/"],
  ["nai-talcor", "NAI TALCOR", "b9b19d2a3f66dfc3bb532e8c5db7399f4db33349", "https://www.naitalcor.com/"],
  ["nai-dominion", "NAI Dominion", "6a78703278580ac43114429ef6f4a0d484167434", "https://www.naidominion.com/"],
] as const;

test("registered Buildout firms exactly preserve all 25 historical public-feed definitions", () => {
  assert.deepEqual(
    REGISTERED_BUILDOUT_FIRMS.map(
      ({ sourceKey, company, pluginKey, listingsPage }) =>
        [sourceKey, company, pluginKey, listingsPage] as const
    ),
    EXPECTED_FIRMS
  );
  assert.deepEqual(
    REGISTERED_BUILDOUT_SOURCE_KEYS,
    EXPECTED_FIRMS.map(([sourceKey]) => sourceKey)
  );
});

test("every registered Buildout firm uses an isolated fail-closed inventory contract", () => {
  for (const definition of REGISTERED_BUILDOUT_FIRMS) {
    assert.equal(definition.inventoryOpts.preferDirectJson, true);
    assert.equal(definition.inventoryOpts.directReferer, definition.listingsPage);
    assert.equal(
      definition.inventoryOpts.inventorySort,
      BUILDOUT_STABLE_INVENTORY_SORT
    );
    assert.equal(definition.inventoryOpts.pageConcurrency, 1);
    assert.equal(definition.inventoryOpts.requireCompletePages, true);
    assert.equal(definition.inventoryOpts.cacheSlug, definition.sourceKey);
    assert.equal(definition.inventoryOpts.usePageCache, true);
    assert.equal(definition.inventoryOpts.recoveryPasses, 1);
    assert.equal(definition.inventoryOpts.recoveryCooldownMs, 15000);
    assert.equal(definition.inventoryOpts.maxRecoveryPages, 60);
  }
});

test("registered Buildout firms have unique tokens and cache namespaces", () => {
  assert.equal(
    new Set(REGISTERED_BUILDOUT_FIRMS.map(({ pluginKey }) => pluginKey)).size,
    REGISTERED_BUILDOUT_FIRMS.length
  );
  assert.equal(
    new Set(
      REGISTERED_BUILDOUT_FIRMS.map(
        ({ inventoryOpts }) => inventoryOpts.cacheSlug
      )
    ).size,
    REGISTERED_BUILDOUT_FIRMS.length
  );
});

test("optional detail configs are validated and available for registry-generated enrichment", () => {
  for (const definition of REGISTERED_BUILDOUT_FIRMS) {
    assert.doesNotThrow(() => assertRegisteredBuildoutFirm(definition));
    assert.equal(
      definition.detailConfig?.host,
      new URL(definition.listingsPage).hostname
    );
    assert.deepEqual(definition.detailConfig?.listingHosts, [
      new URL(definition.listingsPage).hostname,
    ]);
    assert.equal(definition.detailConfig?.pluginKey, definition.pluginKey);
    assert.equal(definition.detailConfig?.company, definition.company);
    const iframeUrl = buildoutDetailIframeUrl(
      definition.sourceKey,
      `${definition.listingsPage}?propertyId=listing-123-sale`,
      definition.detailConfig
    );
    assert.equal(
      iframeUrl,
      `https://buildout.com/plugins/${definition.pluginKey}/` +
        `${definition.detailConfig?.host}/inventory/listing-123` +
        "?pluginId=0&iframe=true&embedded=true"
    );
  }

  const base = REGISTERED_BUILDOUT_FIRMS[0] as RegisteredBuildoutFirm;
  assert.doesNotThrow(() =>
    assertRegisteredBuildoutFirm({ ...base, detailConfig: undefined })
  );
  assert.throws(
    () =>
      assertRegisteredBuildoutFirm({
        ...base,
        detailConfig: { ...base.detailConfig!, host: "attacker.example" },
      }),
    /detail host must exactly match/
  );
  assert.throws(
    () =>
      assertRegisteredBuildoutFirm({
        ...base,
        detailConfig: { ...base.detailConfig!, listingHosts: [] },
      }),
    /detail listing hosts must be unique lowercase hosts/
  );
  assert.throws(
    () =>
      assertRegisteredBuildoutFirm({
        ...base,
        detailConfig: {
          ...base.detailConfig!,
          listingHosts: ["attacker.example"],
        },
      }),
    /detail listing hosts must be unique lowercase hosts/
  );
  assert.throws(
    () => assertRegisteredBuildoutFirm({ ...base, pluginKey: "not-a-token" }),
    /40 lowercase hex/
  );
  assert.throws(
    () =>
      assertRegisteredBuildoutFirm({
        ...base,
        detailConfig: {
          ...base.detailConfig!,
          pluginKey: REGISTERED_BUILDOUT_FIRMS[1].pluginKey,
        },
      }),
    /detail plugin key must equal/
  );
  assert.throws(
    () =>
      assertRegisteredBuildoutFirm({
        ...base,
        detailConfig: { ...base.detailConfig!, company: "Wrong Company" },
      }),
    /detail company must equal/
  );
});

test("registeredBuildoutFirmFor resolves every exact key and rejects other sources", () => {
  for (const [sourceKey, , pluginKey] of EXPECTED_FIRMS) {
    const resolved = registeredBuildoutFirmFor(sourceKey);
    assert.ok(resolved);
    assert.equal(resolved.pluginKey, pluginKey);
  }
  assert.equal(registeredBuildoutFirmFor("cbre"), null);
});

test("registry definitions remain assignable to the shared Buildout contract", () => {
  const definitions: readonly RegisteredBuildoutFirm[] =
    REGISTERED_BUILDOUT_FIRMS;
  assert.equal(definitions.length, 25);
});
