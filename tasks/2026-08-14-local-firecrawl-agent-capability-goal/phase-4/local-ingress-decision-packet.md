# AGENTIC-2284: Local Firecrawl Ingress Decision Packet

**Date:** 2026-08-14
**Scope:** decision and threat-model research only. This packet makes no code,
runtime, Docker, `.env`, network, CRE, Linear, or deployment change.

## Decision

Adopt **local/private-only as the default posture**. Do not expose the local
Firecrawl API to a LAN, VPN, tunnel, reverse proxy, or public Internet until a
founder-approved ingress design is implemented and verified.

The checked-in Compose API mapping is configurable as
`${PORT:-3002}:${INTERNAL_PORT:-3002}` and the service listens inside its
container on `0.0.0.0`. That source configuration does not prove the host’s
current binding or firewall state, but it also does not enforce a loopback-only
binding. `SELF_HOST.md` correctly says the default API is unauthenticated and
requires authentication, TLS termination, and network policy before it leaves
a trusted network. Treat that as a release gate, not a documentation footnote.

## Threat model and attack paths

| Path | What can go wrong | Required control class |
| --- | --- | --- |
| Untrusted client reaches API port | Anonymous callers submit scrape/crawl/parse/extract work, consume resources or provider cost, and retrieve output. | Authenticated front door, authorization, quota/rate limits, and audit identity. |
| Public API request drives a fetch engine | Caller-controlled targets can cause SSRF-like access, outbound abuse, or scraping-policy violations from the host’s network identity. | Request policy, egress controls, cost/concurrency ceilings, abuse monitoring, and incident response. |
| A proxy/tunnel changes reachability | A local-only claim becomes false through a mesh VPN, port-forward, reverse proxy, or router/NAT change. | Explicit inventory and enforcement of every ingress path, with repeatable external verification. |
| API and queue share a host | Untrusted work interferes with local operator or CRE-adjacent workloads, or error/log paths retain sensitive request data. | Workload isolation, queue policy, safe logging/retention, and a deliberate CRE separation proof. |
| Dependencies are later published | Redis, RabbitMQ, Postgres, worker, OCR, or management surfaces become reachable even if the API is protected. | Private network defaults, published-port inventory, and separate authentication/hardening per dependency. |
| TLS/auth proxy becomes the boundary | Header spoofing, path bypass, missing websocket/stream coverage, stale keys, or direct port access bypass the proxy. | Deny direct API access; tested TLS, authn/authz, key rotation, and end-to-end route coverage. |

The highest-risk error is treating reachability as a convenience change. An
unauthenticated scrape API is an execution and egress capability, not merely a
read-only status endpoint.

## Bounded options

1. **Loopback-only local operations (recommended now).** Enforce binding to
   `127.0.0.1:3002` (and only explicitly required local adapter ports) through
   a reviewed Compose/host configuration. No tunnel, router rule, mesh share,
   or reverse proxy. This is the only option compatible with the current
   unauthenticated local helper/CLI/MCP assumptions.
2. **Named private-network operator access.** Put a separate, authenticated
   TLS front door in front of a non-public API. Require a small named-user
   allowlist, deny direct port access, use short-lived credentials or mTLS,
   retain only body-free audit metadata, and apply per-identity limits. This
   requires an operational owner and secure key lifecycle.
3. **Public service.** Treat as a production product launch: authn/authz,
   tenancy decision, endpoint allowlist, WAF/DDoS posture, rate and cost
   budgets, outbound target policy, secret management, persistence/backups,
   observability, incident response, legal/data-use review, and rollback.
   This is out of scope for the local fork without a separate approved design.

Do not solve option 2 or 3 by simply publishing the existing port or putting a
generic tunnel in front of it. The API must be unreachable except through the
chosen enforcement boundary.

## Required evidence and tests

For option 1:

- Render the exact Compose configuration and verify an explicit loopback bind;
  do not infer it from a successful `localhost` request.
- From the host, verify the intended port answers locally. From an authorized
  second device/network namespace, verify it is unreachable. Record only
  body-free connection results.
- Inventory published ports for all Compose services and the OCR adapter.
  Redis, RabbitMQ, Postgres, workers, and management UIs must remain private.
- Run a non-mutating health/preflight check and the focused agent-safe negative
  suite. Do not use a POST as a reachability check.

For option 2 or 3, add all of the above plus:

- unauthenticated, expired, malformed, wrong-audience, and revoked credential
  requests fail before an API handler executes;
- authorized requests cannot bypass the proxy by direct host port, alternate
  hostname, IPv6, forwarded header, websocket/stream, or a path-normalization
  variant;
- TLS chain, hostname, redirect, and certificate rotation are tested from an
  independent client; no secret appears in the test artifact;
- per-identity concurrency, rate, body-size, page, wall-clock, and provider
  cost caps work under contention and fail closed;
- target/egress policy blocks internal addresses, redirects, DNS-rebinding,
  proxy escape, and disallowed content classes without contacting real
  internal targets;
- queue isolation and a CRE regression check prove external work cannot touch
  collector schedules, data writes, listing records, OM facts, or EQUIRE
  consumer paths;
- a rollback drill removes public reachability and proves access/audit logs
  remain appropriately retained and redacted.

## Explicit approval gates

Keep **Needs Cayman** and **Human-Signoff** on AGENTIC-2284 until Cayman
selects an option and approves these facts:

- allowed users, networks, devices, and whether remote agents are in scope;
- data classification, retention, and external scraping/model-provider rules;
- identity provider or mTLS/key-management owner, quota/cost owner, and
  incident/rollback owner;
- service availability expectations, backup expectations, and upgrade window;
- network owner approval for firewall, VPN, reverse-proxy, tunnel, DNS, or
  router changes; and
- confirmation that CRE remains separate and no collector runtime or database
  capability is exposed through this ingress.

Only after the design, tests, and an independent security review satisfy those
gates should a change be proposed on this same consolidated branch.

## Relationship to AGENTIC-2279

The agent-safe pilot assumes a loopback API and verifies a fixed local origin
for its own request path. It does not authenticate callers to Firecrawl and
does not establish that Docker’s published port is loopback-bound. A general
sandbox therefore cannot use the pilot as justification for remote access; a
remote sandbox requires the ingress decision first, and local fixture work
remains local-only.

## Why no silent implementation

Ingress changes alter who may spend shared capacity, cause outbound fetches,
and access returned material. They also depend on host networking, TLS,
identity, credential lifecycle, and operations ownership that source code
cannot safely infer. A premature port binding, reverse proxy, or tunnel could
either accidentally expose an unauthenticated service or disrupt local
operator and CRE workflows. This packet preserves the safe default and makes
the human decisions and proof obligations explicit.
