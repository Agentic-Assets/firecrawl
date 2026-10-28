Part of [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

# NAI native page capture

Only `CRE_NAI_NATIVE_CAPTURE_DIR` explicitly enables capture. The caller must
supply a private owner-controlled attempt directory. Each actual publicPosts
request publishes one owner-only atomic JSON page named by request offset and
SHA-256 of the complete UTF-8 JSON entry including its final newline. It carries the exact request JSON string and the
Fetch-delivered response body as base64, each with a byte count and digest,
plus request/final URL, status and observation clocks. These bytes are after
HTTP transport decoding and are not wire bytes; identity, publication authority,
visibility and completeness remain unverified. All returned data, including
GraphQL errors, stays in the body. The current query cannot capture fields not
requested by GraphQL.

The 16 MiB response ceiling and 64 KiB request ceiling are fail-closed. An
existing page is accepted only when its complete bytes exactly match; different
responses or observation clocks produce distinct entries. Failure produces a redacted
error and leaves previous pages intact. This module does not write a database,
start a worker or authorize retention. The opted-in checkpoint coordinator binds
and validates these entries, including request scope, after worker settlement.
It uses the Python acquisition converter and existing foreground evidence lane;
see [`source_evidence/README.md`](../../source_evidence/README.md) for policy,
recovery and database boundaries.

The coordinator must assert `CRE_NAI_NATIVE_CAPTURE_SINGLE_WRITER=1` only when
it owns one worker for the attempt directory. Cross-process shared publication
is unsupported and is not protected by an OS lock. Within that worker all quota
checks and publication are synchronous. `CRE_NAI_NATIVE_CAPTURE_MAX_PAGES` and
`CRE_NAI_NATIVE_CAPTURE_MAX_BYTES` may lower the defaults of 128 pages and
128 MiB of exact serialized entry bytes. Unknown, symlinked, multiply linked,
or temporary entries refuse publication rather than being ignored. Quota
refusal leaves previously published pages intact; exact replay adds no bytes.

## Draft Buildout parsed inventory

An explicit v2 policy may enable `buildout_inventory_parsed_v1` for the 25
registered source/provider tuples. SVN, Lee and Franklin coordinate families
are excluded. The existing source request is captured after JavaScript parsing;
no new fetch is introduced. The representation is not original HTTP bytes or
original numeric precision. Capture time and the nullable, possibly older
source observation time are distinct; status and final URL are not fabricated.
An in-memory cache hit creates no fresh page capture.

The private attempt is limited to 128 pages and 128 MiB, with 16 MiB per parsed
body. A capture failure stops the enabled path. Database admission remains
separate: existing source and credential policies apply, and Faris collector
projection does not sanitize native pages. The evidence lane also limits each
fact to 3.5 MiB, each observation to 16 MiB and each page to 10,000 rows.
This draft is unverified under the hold and not deployed; it proves no source
acceptance, complete inventory, identity or freshness.

## Avison Young website feed

When the foreground coordinator supplies a private attempt directory through
`CRE_AVISON_NATIVE_CAPTURE_DIR` and asserts
`CRE_AVISON_NATIVE_CAPTURE_SINGLE_WRITER=1`, the website feed publishes before
JSON parsing or listing mapping. The optional `CRE_AVISON_NATIVE_CAPTURE_MAX_PAGES`
and `CRE_AVISON_NATIVE_CAPTURE_MAX_BYTES` lower the shared 128-page and 128 MiB
limits. The publisher shares the bounded atomic private-file custody mechanism
with NAI. The feed body is collector-decoded text re-encoded as UTF-8; it is not
claimed to be the original Fetch or wire bytes. The existing transport reports a
successful response without exposing its exact HTTP status or final URL, so the
receipt records the attempted request and that limitation. The integration key
and request headers are never added to the page or its receipt; a body containing
the key, including JSON Unicode escapes, is refused with a redacted error.

These pages are private local raw custody, not database admission. Avison's
source-owned asking-price visibility projection and existing source and
credential policies must be applied before retained evidence is written. The
private filename hash is a local custody digest and must not be carried into a
projected database observation when source content is withheld. No identity,
visibility, completeness, or publisher-authentication claim follows from capture.
