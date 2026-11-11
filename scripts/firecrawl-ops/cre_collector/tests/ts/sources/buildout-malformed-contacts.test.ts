import assert from "node:assert/strict";
import test from "node:test";
import { brokers, resetBrokerStateForTests } from "../../../lib/broker.js";
import { prune } from "../../../lib/util.js";
import { buildoutCache, buildoutQueryFingerprint, srcBuildout } from "../../../sources/buildout.js";
import { preserveBuildoutRaw } from "../../../sources/pure/buildout-raw-retention.js";

test("malformed optional contacts do not stop Buildout listings or valid contacts", async () => {
  const key = "fixture-malformed-contacts";
  const memoryKey = `${key}\u0000${buildoutQueryFingerprint({})}`;
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error("unexpected network request"); };
  resetBrokerStateForTests();
  const rows = [
    { id: 1, sale: true, show_link: "https://example.test/?propertyId=1", broker_contacts: { unexpected: "shape" } },
    { id: 2, sale: true, show_link: "https://example.test/?propertyId=2",
      broker_contacts: [null, "invalid", { name: "Valid Broker", phone: 123 }, { email: "valid@example.test" }] },
    { id: 3, sale: true, show_link: "https://example.test/?propertyId=3", broker_contacts: [{ name: "Peer Broker" }] },
    { id: 4, sale: true, show_link: "https://example.test/?propertyId=4",
      broker_contacts: Array.from({length: 1001}, (_, i) => ({name: `Broker ${i}`})) },
    { id: 5, sale: true, show_link: "https://example.test/?propertyId=5",
      broker_contacts: [{name: "Large Raw Broker"}], oversized: "x".repeat(1_000_001) },
  ];
  buildoutCache.set(memoryKey, { total: rows.length, items: rows });
  try {
    const result = await srcBuildout("Fixture", key, "https://example.test", "sale", Infinity, false);
    assert.equal(result.listings.length, 5);
    assert.deepEqual(result.listings[0].buildoutContactMapping,
      { invalidContainer: true });
    assert.deepEqual(result.listings[1].buildoutContactMapping,
      { invalidEntryCount: 3 });
    assert.equal(result.listings[3].brokerIds.length, 1001);
    assert.equal(brokers[result.listings[3].brokerIds[1000]].name, "Broker 1000");
    assert.deepEqual(result.listings[1].brokerIds.map((id: number) => brokers[id].name), ["Valid Broker", null]);
    assert.equal(brokers[result.listings[1].brokerIds[0]].phone, null);
    assert.equal(brokers[result.listings[2].brokerIds[0]].name, "Peer Broker");
    assert.equal(result.listings[2].buildoutContactMapping, undefined);
    for (let i = 0; i < rows.length; i++) {
      const original = result.listings[i];
      const artifact = prune({sourceKey: key, ...original});
      preserveBuildoutRaw(artifact, original.rawBuildout, original.buildoutRawRetention, key);
      assert.equal(artifact.preserveChildCollections, true);
      assert.deepEqual(artifact.buildoutContactMapping, original.buildoutContactMapping);
      if (i < rows.length - 1) {
        assert.deepEqual(artifact.rawBuildout.broker_contacts, rows[i].broker_contacts);
        assert.equal(artifact.buildoutRawRetention.status, "complete");
      } else {
        assert.equal(artifact.rawBuildout, undefined);
        assert.equal(artifact.buildoutRawRetention.reason, "invalid_or_over_limit");
        assert.equal(brokers[artifact.brokerIds[0]].name, "Large Raw Broker");
      }
    }
  } finally {
    buildoutCache.delete(memoryKey);
    globalThis.fetch = oldFetch;
    resetBrokerStateForTests();
  }
});
