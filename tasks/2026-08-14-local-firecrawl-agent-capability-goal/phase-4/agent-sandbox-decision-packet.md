# AGENTIC-2283: Local Agent Sandbox Decision Packet

**Date:** 2026-08-14
**Scope:** decision and threat-model research only. This packet makes no code,
runtime, Docker, `.env`, network, CRE, Linear, or deployment change.

## Decision

Do **not** silently add a general local-agent sandbox. Keep the current
`firecrawl_request.py --agent-safe` interface as an intentionally narrow
fixture pilot only: canonical `https://example.com/`, one tracked synthetic
PDF, loopback API, bounded one-page operations, no AI/OCR/profile controls,
fresh body-free prerequisite evidence, and a fixed receipt location.

That is a useful product-safety constraint, but it is not a sandbox for an
arbitrary agent, arbitrary URL, arbitrary local file, or another user. The
default for all of those remains **not authorized**.

## Threat model and attack paths

| Path | What can go wrong | Required control class |
| --- | --- | --- |
| Agent input to Firecrawl fetch | A URL, redirect, DNS rebinding, proxy, or browser request reaches loopback, RFC1918, link-local, Docker, VPN, or cloud-metadata services. | Enforced network egress policy and fixed target authorization, not parser checks alone. |
| Agent local-file input to parse | A secret, ignored file, governed CRE artifact, or path outside the intended fixture is uploaded to the API. | File-system isolation with an approved read-only fixture mount and no inherited home, `.env`, credential, or CRE paths. |
| Agent request to shared API/queue | It starts expensive work, interferes with an active crawl, learns or polls another job handle, or starves a CRE-adjacent workload. | Per-run identity/ownership, queue/cost caps, idle or reserved execution lane, and non-retry stop rules. |
| Agent controls to local helper/wrappers | It changes model/OCR/Docker settings, leaks provider configuration, or expands output retention. | Separate operator-only mutation entrypoint; the sandboxed surface has no mutation flag, socket, or secret environment. |
| Receipt/log/result boundary | Server-controlled fields, source content, URLs, paths, headers, or job IDs enter durable artifacts or terminal output. | Closed body-free schemas, allowlisted metrics, opaque references, redaction tests, and bounded retention. |
| Tool server/MCP boundary | A prompt-controlled client invokes broad MCP/CLI tools outside a reviewed recipe. | Explicit tool allowlist and launch profile; safe mode is not conferred by an agent's self-description. |

The shared-UID local-machine threat is material: a command-line `--agent-safe`
flag is not authentication or a defense against a malicious process with the
same user account. A real sandbox must enforce isolation below the agent's
process, not rely on callers to retain an agreed command shape.

## Bounded options

1. **Fixture-only pilot (recommended now).** Retain the existing exact-fixture
   agent-safe path. It performs no general host authorization and must remain
   unavailable for CRE acquisition, arbitrary public URLs, or arbitrary files.
2. **Preapproved local research runner.** After founder approval, launch a
   dedicated process/container with a read-only fixture directory, empty
   credentials, no Docker socket, no home-directory mount, a fresh task
   directory, and an egress policy that permits only explicitly approved
   destinations. It should talk to a scoped Firecrawl front door, not the
   unconstrained host API.
3. **Offline parse-only lane.** Permit only a catalogued, checksum-pinned,
   non-sensitive fixture corpus mounted read-only. This does not include a
   general repository or user-files path.
4. **Multi-user or remote-agent service.** Treat this as a new product
   surface, requiring authenticated identities, authorization policy, tenancy
   separation, rate/cost limits, audit retention, incident response, and a
   separate ingress decision. It is not an extension of the local helper.

Options 2 through 4 need a written threat model and a separately approved
implementation packet. Do not introduce a second local client or an ad-hoc
URL resolver while deciding.

## Minimum design and test evidence before any wider option

- A written target/file authorization model naming the actor, allowed inputs,
  maximum work, retention rule, and stop/rollback behavior.
- A process-level egress proof: rejected loopback, private, link-local,
  metadata, redirect, DNS-rebinding, and proxy routes cannot leave the
  sandbox. Use synthetic local fixtures for negative tests; do not probe real
  internal systems.
- A file-isolation proof that unique secret sentinels placed outside the
  approved fixture cannot be read, uploaded, logged, hashed, or mounted.
- Positive and negative queue tests proving a run cannot submit when state is
  active/unknown, cannot retry submission automatically, and cannot poll a
  job it did not submit in the same bounded process.
- Receipt/terminal inspection proving body, URL/query, header, absolute path,
  secret, and job-handle sentinels never survive. Include interruption and
  malformed-server-response cases.
- A resource test for page, concurrency, wall-clock, disk, memory, request,
  and provider-cost ceilings. Termination must clean temporary state without
  silently retaining work or changing shared profiles.
- A CRE regression proof: no collector, EQUIRE, Supabase/Postgres, scheduler,
  listing artifact, or OM-facts path is mounted, called, or modified.

## Explicit approval gates

Before implementation, keep **Needs Cayman** and **Human-Signoff** on the
issue and obtain Cayman’s recorded decision on all of the following:

- whether the authorized actor is a trusted same-user local agent only, a
  named local service, or a remote/multi-user actor;
- whether any network destination beyond the exact fixture is permitted;
- data classes that may be mounted, fetched, retained, or passed to a model;
- the accountable operator, cost ceiling, incident owner, and rollback path;
- whether a scoped Firecrawl front door is required before sandbox work.

The decision also requires a security review of the proposed enforcement
layer. An application-level argument validator alone does not satisfy this
gate.

## Relationship to AGENTIC-2279

The exact-fixture `--agent-safe` pilot is evidence that a restricted recipe can
fail closed before an operation. It is neither a general SSRF control nor proof
of filesystem, process, identity, or network isolation. Keep its fixed
fixtures and body-free receipts as the baseline regression suite; do not widen
its URL/file rules in the name of sandboxing.

## Why no silent implementation

Sandboxing changes trust boundaries, reachable data, network authority, and
operator responsibility. The correct controls depend on whether this is a
single trusted Mac workflow or a multi-user service, and may require a new
front door and host-network configuration. Implementing any option without
that choice could create a misleading safety claim or change the shared local
Firecrawl/CRE operating posture. This packet intentionally records the
decision point rather than treating an unapproved technical default as policy.
