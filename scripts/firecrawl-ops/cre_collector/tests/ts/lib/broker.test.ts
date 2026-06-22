import { describe, it, beforeEach } from "node:test";
import assert from "node:assert/strict";
import {
  brokerRef,
  brokers,
  resetBrokerStateForTests,
} from "../../../lib/broker.js";

describe("brokerRef", () => {
  beforeEach(() => {
    resetBrokerStateForTests();
  });

  it("returns null when no name and no email", () => {
    assert.equal(
      brokerRef({ name: null, company: "CBRE" }),
      null,
    );
    assert.equal(brokers.length, 0);
  });

  it("assigns a new index for a first broker with name only", () => {
    const idx = brokerRef({ name: "Jane Doe", company: "JLL" });
    assert.equal(idx, 0);
    assert.equal(brokers.length, 1);
    assert.deepEqual(brokers[0], {
      name: "Jane Doe",
      email: null,
      phone: null,
      office: null,
      avatarUrl: null,
      company: "JLL",
    });
  });

  it("assigns a new index for email-only broker", () => {
    const idx = brokerRef({
      name: null,
      email: "broker@example.com",
      company: "Newmark",
    });
    assert.equal(idx, 0);
    assert.equal(brokers[0]!.email, "broker@example.com");
  });

  it("dedupes by email|name|company key", () => {
    const first = brokerRef({
      name: "Sam Lee",
      email: "sam@cbre.com",
      company: "CBRE",
    });
    const second = brokerRef({
      name: "Sam Lee",
      email: "sam@cbre.com",
      company: "CBRE",
    });
    assert.equal(second, first);
    assert.equal(brokers.length, 1);
  });

  it("treats different company as a separate broker", () => {
    const a = brokerRef({ name: "Alex", email: "alex@x.com", company: "CBRE" });
    const b = brokerRef({ name: "Alex", email: "alex@x.com", company: "JLL" });
    assert.notEqual(a, b);
    assert.equal(brokers.length, 2);
  });

  it("merges phone, office, and avatar on duplicate key when missing", () => {
    const idx = brokerRef({
      name: "Pat Kim",
      email: "pat@svn.com",
      company: "SVN",
    });
    brokerRef({
      name: "Pat Kim",
      email: "pat@svn.com",
      company: "SVN",
      phone: "555-0100",
      office: "Dallas",
      avatarUrl: "https://cdn.example/avatar.png",
    });
    assert.equal(brokers[idx!]!.phone, "555-0100");
    assert.equal(brokers[idx!]!.office, "Dallas");
    assert.equal(brokers[idx!]!.avatarUrl, "https://cdn.example/avatar.png");
  });

  it("does not overwrite existing phone, office, or avatar", () => {
    const idx = brokerRef({
      name: "Riley",
      email: "riley@ay.com",
      company: "Avison Young",
      phone: "555-1000",
      office: "Chicago",
      avatarUrl: "https://cdn.example/original.png",
    });
    brokerRef({
      name: "Riley",
      email: "riley@ay.com",
      company: "Avison Young",
      phone: "555-9999",
      office: "Houston",
      avatarUrl: "https://cdn.example/other.png",
    });
    assert.equal(brokers[idx!]!.phone, "555-1000");
    assert.equal(brokers[idx!]!.office, "Chicago");
    assert.equal(brokers[idx!]!.avatarUrl, "https://cdn.example/original.png");
  });
});
