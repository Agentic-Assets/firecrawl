import assert from "node:assert/strict";
import test from "node:test";
import { isInternalHost, TargetDnsUnavailableError } from "./target_dns";

test("DNS errors and empty resolution fail closed with a distinct transient error", async () => {
  await assert.rejects(
    isInternalHost("property.example", async () => {
      throw new Error("EAI_AGAIN");
    }),
    TargetDnsUnavailableError,
  );
  await assert.rejects(
    isInternalHost("property.example", async () => []),
    TargetDnsUnavailableError,
  );
});

test("private, mapped, mixed and public answers keep SSRF classification", async () => {
  const resolverMustNotRun = async () => {
    throw new Error("literal addresses must not be resolved");
  };
  for (const literal of [
    "127.0.0.1",
    "::1",
    "10.1.2.3",
    "169.254.169.254",
    "::ffff:127.0.0.1",
    "0.0.0.0",
  ]) {
    assert.equal(
      await isInternalHost(literal, resolverMustNotRun),
      true,
      literal,
    );
  }
  assert.equal(await isInternalHost("", resolverMustNotRun), true);
  for (const answer of ["127.0.0.1", "192.168.1.10", "fd00::1", "::ffff:10.0.0.1"]) {
    assert.equal(
      await isInternalHost("property.example", async () => [
        { address: answer },
      ]),
      true,
      answer,
    );
  }
  assert.equal(
    await isInternalHost("property.example", async () => [
      { address: "8.8.8.8" },
      { address: "10.0.0.1" },
    ]),
    true,
  );
  assert.equal(
    await isInternalHost("Property.Example.", async (host) => {
      assert.equal(host, "property.example");
      return [{ address: "8.8.8.8" }];
    }),
    false,
  );
});

test("IPv6 literal URL hosts are classified without DNS", async () => {
  const resolverMustNotRun = async () => {
    throw new Error("IPv6 literals must not be resolved");
  };
  const blocked = [
    "http://[::1]/",
    "http://[::ffff:7f00:1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://[fd00:ec2::254]/",
  ];
  for (const url of blocked) {
    const host = new URL(url).hostname;
    assert.ok(host.startsWith("["), `${url} keeps its brackets: ${host}`);
    assert.equal(await isInternalHost(host, resolverMustNotRun), true, url);
  }
  const publicHost = new URL("https://[2606:4700:4700::1111]/").hostname;
  assert.equal(await isInternalHost(publicHost, resolverMustNotRun), false);
  // Exactly one pair is stripped; anything else bracketed is refused.
  for (const host of ["[[::1]]", "[property.example]", "[127.0.0.1]", "[]"]) {
    assert.equal(await isInternalHost(host, resolverMustNotRun), true, host);
  }
});

test("non-public IPv6 that ipaddr.js calls unicast is internal as a literal and a DNS answer", async () => {
  const resolverMustNotRun = async () => {
    throw new Error("IPv6 literals must not be resolved");
  };
  const internal = [
    "::7f00:1", // IPv4-compatible 127.0.0.1
    "::a9fe:a9fe", // IPv4-compatible 169.254.169.254
    "::808:808", // IPv4-compatible public IPv4: deprecated form, still refused
    "fec0::1", // deprecated site-local
    "64:ff9b:1::7f00:1", // RFC 8215 local-use NAT64
    "64:ff9b:1::a9fe:a9fe",
    "3fff::1", // RFC 9637 documentation
    "5f00::1", // outside 2000::/3 global unicast (RFC 9602 SRv6 SIDs)
    "100:0:0:1::1", // outside 2000::/3 global unicast
    // Already non-unicast in ipaddr.js; kept as regression coverage.
    "2001:db8::1",
    "100::1",
    "2001::1",
    "2002:7f00:1::",
    "2002:808:808::",
  ];
  for (const address of internal) {
    const host = new URL(`http://[${address}]/`).hostname;
    assert.equal(await isInternalHost(host, resolverMustNotRun), true, host);
    assert.equal(
      await isInternalHost("property.example", async () => [{ address }]),
      true,
      `DNS answer ${address}`,
    );
  }
  for (const address of [
    "2606:4700:4700::1111",
    "2001:4860:4860::8888",
    "2620:fe::fe",
    "2a00:1450:4001:80b::200e",
  ]) {
    const host = new URL(`https://[${address}]/`).hostname;
    assert.equal(await isInternalHost(host, resolverMustNotRun), false, host);
    assert.equal(
      await isInternalHost("property.example", async () => [{ address }]),
      false,
      `DNS answer ${address}`,
    );
  }
});
